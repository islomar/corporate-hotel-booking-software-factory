import contextlib
import io
import json
import os
import sys
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))

import warn_edit_loops as hook

ROOT = Path(__file__).resolve().parents[2]


def _event(file_path: str, *, session: str = "s-1", tool: str = "Edit", cwd: str = "/repo") -> str:
    return json.dumps(
        {
            "session_id": session,
            "hook_event_name": "PostToolUse",
            "tool_name": tool,
            "cwd": cwd,
            "tool_input": {"file_path": file_path},
        }
    )


class LoopDetectorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.state = tempfile.TemporaryDirectory()
        self.addCleanup(self.state.cleanup)
        patcher = mock.patch.dict(os.environ, {hook.STATE_DIR_ENV: self.state.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _main(self, stdin: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(stdin)), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = hook.main()
        return code, out.getvalue(), err.getvalue()

    def _edits(self, n: int, file_path: str = "/repo/src/a.py", **kw: str) -> list[tuple[int, str, str]]:
        return [self._main(_event(file_path, **kw)) for _ in range(n)]

    def _context(self, out: str) -> str:
        payload = json.loads(out)
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        return payload["hookSpecificOutput"]["additionalContext"]

    def test_the_first_four_edits_are_silent(self) -> None:
        self.assertEqual(self._edits(4), [(0, "", "")] * 4)

    def test_the_5th_edit_adds_context(self) -> None:
        code, out, err = self._edits(5)[-1]

        self.assertEqual((code, err), (0, ""))
        self.assertEqual(
            self._context(out),
            "5th edit of src/a.py this session: step back, re-read the failing test and the plan before editing again.",
        )

    def test_the_8th_edit_adds_context_and_the_ones_between_are_silent(self) -> None:
        results = self._edits(9)

        self.assertEqual(results[5:7], [(0, "", "")] * 2)
        self.assertEqual(results[8], (0, "", ""))
        self.assertTrue(self._context(results[7][1]).startswith("8th edit of src/a.py this session: step back"))

    def test_write_edit_and_multiedit_count_together(self) -> None:
        for tool in ["Write", "Edit", "MultiEdit", "Edit"]:
            self._main(_event("/repo/src/a.py", tool=tool))

        self.assertIn("5th edit", self._context(self._main(_event("/repo/src/a.py", tool="MultiEdit"))[1]))

    def test_a_relative_and_an_absolute_path_are_the_same_file(self) -> None:
        self._edits(2, "src/a.py")
        self._edits(2, "/repo/src/../src/a.py")

        self.assertIn("5th edit", self._context(self._main(_event("/repo/src/a.py"))[1]))

    def test_files_are_counted_apart(self) -> None:
        self._edits(4, "/repo/src/a.py")

        self.assertEqual(self._main(_event("/repo/src/b.py")), (0, "", ""))

    def test_sessions_are_counted_apart(self) -> None:
        self._edits(4, session="s-1")

        self.assertEqual(self._main(_event("/repo/src/a.py", session="s-2")), (0, "", ""))

    def test_a_file_outside_the_working_directory_is_named_by_its_absolute_path(self) -> None:
        self._edits(4, "/elsewhere/x.md")

        self.assertIn("5th edit of /elsewhere/x.md this session", self._context(self._main(_event("/elsewhere/x.md"))[1]))

    def test_a_session_id_cannot_escape_the_state_folder(self) -> None:
        self._edits(1, session="../../etc/evil")

        self.assertEqual([p.parent for p in Path(self.state.name).rglob("*") if p.is_file()], [Path(self.state.name)])

    def test_bad_input_never_blocks_and_prints_nothing(self) -> None:
        for stdin in ["not json", "[]", json.dumps({"tool_input": {"file_path": "/x"}}), json.dumps({"session_id": "s", "tool_input": {}})]:
            with self.subTest(stdin):
                self.assertEqual(self._main(stdin), (0, "", ""))

    def test_an_unreadable_state_file_starts_the_count_again(self) -> None:
        self._edits(4)
        for f in Path(self.state.name).iterdir():
            f.write_text("garbage")

        self.assertEqual(self._main(_event("/repo/src/a.py")), (0, "", ""))

    def test_the_default_folder_is_per_user(self) -> None:
        with mock.patch.dict(os.environ, {hook.STATE_DIR_ENV: ""}):
            self.assertTrue(hook.state_dir().endswith(f"chb-edit-loops-{os.getuid()}"))

    def test_a_folder_open_to_other_users_is_refused_without_blocking(self) -> None:
        os.chmod(self.state.name, 0o777)
        self.addCleanup(os.chmod, self.state.name, 0o700)

        with self.assertRaises(PermissionError):
            hook.record_edit(self.state.name, "s-1", "/repo/a.py")
        self.assertEqual(self._edits(5)[-1], (0, "", ""))

    def test_a_symlinked_folder_is_refused(self) -> None:
        link = Path(self.state.name) / "link"
        target = Path(self.state.name) / "real"
        target.mkdir(mode=0o700)
        link.symlink_to(target)

        with self.assertRaises(PermissionError):
            hook.record_edit(str(link), "s-1", "/repo/a.py")

    def test_a_symlinked_state_file_is_not_followed(self) -> None:
        victim = Path(self.state.name) / "victim.txt"
        victim.write_text("keep")
        (Path(self.state.name) / "s-1.json").symlink_to(victim)

        with self.assertRaises(OSError):
            hook.record_edit(self.state.name, "s-1", "/repo/a.py")
        self.assertEqual(victim.read_text(), "keep")

    def test_an_error_never_blocks(self) -> None:
        with mock.patch.object(hook, "record_edit", side_effect=OSError("disk full")):
            self.assertEqual(self._main(_event("/repo/src/a.py")), (0, "", ""))



class PruneTest(unittest.TestCase):
    """CHB-138: `make clean-dev` removes state files older than 7 days."""

    NOW = 1_800_000_000.0
    DAY = 86_400

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / "chb-edit-loops-test"
        self.folder.mkdir(mode=0o700)

    def _file(self, name: str, age_days: float, now: float | None = None) -> Path:
        path = self.folder / name
        path.write_text("{}")
        when = (now or self.NOW) - age_days * self.DAY
        os.utime(path, (when, when))
        return path

    def test_state_files_older_than_7_days_are_removed_and_newer_ones_stay(self) -> None:
        old, new = self._file("old.json", 8), self._file("new.json", 6)

        removed = hook.remove_stale(str(self.folder), now=self.NOW)

        self.assertEqual(removed, [str(old)])
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())

    def test_only_json_files_are_removed_and_symlinks_are_not_followed(self) -> None:
        other = self._file("notes.txt", 30)
        victim = Path(self.tmp.name) / "victim.json"
        victim.write_text("keep")
        os.utime(victim, (self.NOW - 30 * self.DAY, self.NOW - 30 * self.DAY))
        (self.folder / "link.json").symlink_to(victim)

        self.assertEqual(hook.remove_stale(str(self.folder), now=self.NOW), [])
        self.assertTrue(other.exists())
        self.assertEqual(victim.read_text(), "keep")

    def test_a_file_removed_meanwhile_is_skipped(self) -> None:
        gone, old = self._file("gone.json", 8), self._file("old.json", 8)
        real_lstat = os.lstat

        def lstat_after_another_prune(path: str) -> os.stat_result:
            if path == str(gone):
                gone.unlink(missing_ok=True)
            return real_lstat(path)

        with mock.patch.object(hook.os, "lstat", side_effect=lstat_after_another_prune):
            self.assertEqual(hook.remove_stale(str(self.folder), now=self.NOW), [str(old)])

    def test_a_missing_folder_is_not_created(self) -> None:
        missing = Path(self.tmp.name) / "missing"

        self.assertEqual(hook.remove_stale(str(missing), now=self.NOW), [])
        self.assertFalse(missing.exists())

    def test_a_folder_open_to_other_users_is_refused(self) -> None:
        self._file("old.json", 8)
        os.chmod(self.folder, 0o777)
        self.addCleanup(os.chmod, self.folder, 0o700)

        with self.assertRaises(PermissionError):
            hook.remove_stale(str(self.folder), now=self.NOW)

    def test_the_prune_flag_reports_an_unsafe_folder_in_one_line(self) -> None:
        os.chmod(self.folder, 0o777)
        self.addCleanup(os.chmod, self.folder, 0o700)
        err = io.StringIO()

        with mock.patch.dict(os.environ, {hook.STATE_DIR_ENV: str(self.folder)}), contextlib.redirect_stderr(err):
            code = hook.main(["--prune"])

        self.assertEqual(code, 1)
        self.assertEqual(err.getvalue(), f"warn_edit_loops: prune refused: {self.folder} is open to other users\n")

    def test_the_prune_flag_cleans_the_configured_folder(self) -> None:
        old = self._file("old.json", 8, now=time.time())
        new = self._file("new.json", 0, now=time.time())

        result = subprocess.run(
            [sys.executable, "-I", str(ROOT / "factory" / "hooks" / "warn_edit_loops.py"), "--prune"],
            env={**os.environ, hook.STATE_DIR_ENV: str(self.folder)},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertIn("removed 1 loop-detector state file", result.stdout)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())


class SettingsTest(unittest.TestCase):
    def test_project_settings_register_the_hook_after_file_edits(self) -> None:
        settings = json.loads((ROOT / ".claude" / "settings.json").read_text())
        (group,) = [g for g in settings["hooks"]["PostToolUse"] if "warn_edit_loops.py" in g["hooks"][0]["command"]]

        self.assertEqual(set(group["matcher"].split("|")), {"Write", "Edit", "MultiEdit"})
        self.assertIn("python3 -I", group["hooks"][0]["command"])
        self.assertIn("factory/hooks/warn_edit_loops.py", group["hooks"][0]["command"])


if __name__ == "__main__":
    unittest.main()
