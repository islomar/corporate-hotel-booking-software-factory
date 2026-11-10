import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))

import deny_harness_edits as hook

ROOT = Path(__file__).resolve().parents[2]

JIRA_VIEW_FEATURE = "CHB-20 [Task] Authentication with Identity Platform\nStatus: In Progress\nParent: CHB-19\n\nBody.\n"
JIRA_VIEW_FACTORY = "CHB-30 [Task] Claude Code hooks\nStatus: In Progress\nParent: CHB-27\n"
JIRA_VIEW_NO_PARENT = "CHB-7 [Task] Orphan\nStatus: To Do\n"


def _git_repo(folder: Path, branch: str) -> Path:
    """A repository without commits whose HEAD names the branch: enough for toplevel and branch lookups."""
    folder.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}  # make check runs inside a git hook
    subprocess.run(["git", "init", "-q", str(folder)], check=True, env=env)
    subprocess.run(["git", "-C", str(folder), "symbolic-ref", "HEAD", f"refs/heads/{branch}"], check=True, env=env)
    return folder.resolve()


class HarnessPathTest(unittest.TestCase):
    def test_top_level_harness_folders_are_harness_paths(self) -> None:
        for rel in [".claude/settings.json", "factory/hooks/x.py", ".github/workflows/ci.yml", "factory", ".claude/worktrees/x"]:
            with self.subTest(rel):
                self.assertTrue(hook.harness_path(f"/repo/{rel}", "/repo"))

    def test_other_paths_are_not(self) -> None:
        for path in ["/repo/backend/factory/x.py", "/repo/docs/.claude/x", "/repo/README.md", "/elsewhere/.claude/x", "/repo/factory.md"]:
            with self.subTest(path):
                self.assertFalse(hook.harness_path(path, "/repo"))

    def test_the_cheap_filter_only_looks_at_paths_with_a_harness_segment(self) -> None:
        self.assertTrue(hook.may_be_harness("/home/u/repo/.claude/worktrees/agent-x/backend/a.py"))
        self.assertTrue(hook.may_be_harness("/repo/factory/x.py"))
        self.assertFalse(hook.may_be_harness("/repo/backend/src/app.py"))
        self.assertFalse(hook.may_be_harness("/repo/factory.md"))


class TicketTest(unittest.TestCase):
    def test_a_ticket_branch_gives_its_key(self) -> None:
        self.assertEqual(hook.ticket_of("CHB-20-auth-with-identity-platform"), "CHB-20")

    def test_other_branches_give_none(self) -> None:
        for branch in ["main", "worktree-agent-abc", "dependabot/pip/x", "", "CHB-20", "chb-20-x", "feature/CHB-20-x"]:
            with self.subTest(branch):
                self.assertIsNone(hook.ticket_of(branch))

    def test_the_product_epics_are_the_owner_list(self) -> None:
        self.assertEqual(hook.PRODUCT_EPICS, frozenset({"CHB-4", "CHB-14", "CHB-19", "CHB-50"}))


class JiraParentTest(unittest.TestCase):
    def _run(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> mock.Mock:
        return mock.Mock(return_value=subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr))

    def test_the_parent_line_gives_the_epic(self) -> None:
        with mock.patch.object(hook.subprocess, "run", self._run(JIRA_VIEW_FEATURE)) as run:
            self.assertEqual(hook.jira_parent("CHB-20"), "CHB-19")

        args = run.call_args.args[0]
        self.assertEqual(args[1:], ["view", "CHB-20"])
        self.assertTrue(args[0].endswith("factory/bin/jira"))
        self.assertLessEqual(run.call_args.kwargs["timeout"], 30)

    def test_a_ticket_without_a_parent_gives_none(self) -> None:
        with mock.patch.object(hook.subprocess, "run", self._run(JIRA_VIEW_NO_PARENT)):
            self.assertIsNone(hook.jira_parent("CHB-7"))

    def test_a_failed_read_raises_with_the_detail(self) -> None:
        with mock.patch.object(hook.subprocess, "run", self._run(returncode=3, stderr="jira: no token in the keyring\n")):
            with self.assertRaisesRegex(hook.JiraUnreadable, "no token in the keyring"):
                hook.jira_parent("CHB-20")

    def test_output_for_another_ticket_raises(self) -> None:
        with mock.patch.object(hook.subprocess, "run", self._run(JIRA_VIEW_FACTORY)):
            with self.assertRaises(hook.JiraUnreadable):
                hook.jira_parent("CHB-20")

    def test_a_timeout_raises(self) -> None:
        with mock.patch.object(hook.subprocess, "run", side_effect=subprocess.TimeoutExpired("jira", 20)):
            with self.assertRaisesRegex(hook.JiraUnreadable, "timed out"):
                hook.jira_parent("CHB-20")

    def test_a_missing_wrapper_raises(self) -> None:
        with mock.patch.object(hook.subprocess, "run", side_effect=FileNotFoundError("jira")):
            with self.assertRaises(hook.JiraUnreadable):
                hook.jira_parent("CHB-20")


class CacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = tempfile.TemporaryDirectory()
        self.addCleanup(self.cache.cleanup)
        os.chmod(self.cache.name, 0o700)
        patcher = mock.patch.dict(os.environ, {hook.CACHE_DIR_ENV: self.cache.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_epic_is_read_once_per_branch(self) -> None:
        with mock.patch.object(hook, "jira_parent", return_value="CHB-19") as lookup:
            self.assertEqual(hook.parent_epic("CHB-20-auth", "CHB-20"), "CHB-19")
            self.assertEqual(hook.parent_epic("CHB-20-auth", "CHB-20"), "CHB-19")

        lookup.assert_called_once_with("CHB-20")

    def test_branches_are_cached_apart(self) -> None:
        with mock.patch.object(hook, "jira_parent", side_effect=["CHB-19", "CHB-27"]):
            hook.parent_epic("CHB-20-auth", "CHB-20")

            self.assertEqual(hook.parent_epic("CHB-30-hooks", "CHB-30"), "CHB-27")

    def test_no_parent_is_cached_too(self) -> None:
        with mock.patch.object(hook, "jira_parent", return_value=None) as lookup:
            hook.parent_epic("CHB-7-orphan", "CHB-7")
            self.assertIsNone(hook.parent_epic("CHB-7-orphan", "CHB-7"))

        lookup.assert_called_once()

    def test_a_failure_is_not_cached(self) -> None:
        with mock.patch.object(hook, "jira_parent", side_effect=[hook.JiraUnreadable("down"), "CHB-19"]):
            with self.assertRaises(hook.JiraUnreadable):
                hook.parent_epic("CHB-20-auth", "CHB-20")

            self.assertEqual(hook.parent_epic("CHB-20-auth", "CHB-20"), "CHB-19")

    def test_an_unreadable_cache_file_reads_jira_again(self) -> None:
        with mock.patch.object(hook, "jira_parent", return_value="CHB-19"):
            hook.parent_epic("CHB-20-auth", "CHB-20")
        for f in Path(self.cache.name).iterdir():
            f.write_text("garbage")

        with mock.patch.object(hook, "jira_parent", return_value="CHB-19") as lookup:
            self.assertEqual(hook.parent_epic("CHB-20-auth", "CHB-20"), "CHB-19")
        lookup.assert_called_once()

    def test_a_branch_name_cannot_escape_the_cache_folder(self) -> None:
        with mock.patch.object(hook, "jira_parent", return_value="CHB-19"):
            hook.parent_epic("CHB-1-../../../evil", "CHB-1")

        self.assertEqual([p.parent for p in Path(self.cache.name).rglob("*") if p.is_file()], [Path(self.cache.name)])

    def test_a_cache_folder_open_to_other_users_is_not_used(self) -> None:
        os.chmod(self.cache.name, 0o777)
        self.addCleanup(os.chmod, self.cache.name, 0o700)

        with mock.patch.object(hook, "jira_parent", return_value="CHB-19") as lookup:
            hook.parent_epic("CHB-20-auth", "CHB-20")
            hook.parent_epic("CHB-20-auth", "CHB-20")

        self.assertEqual(lookup.call_count, 2)
        self.assertEqual(list(Path(self.cache.name).iterdir()), [])

    def test_a_symlinked_cache_file_is_not_followed(self) -> None:
        victim = Path(self.cache.name) / "victim.txt"
        victim.write_text(json.dumps({"ticket": "CHB-20", "parent": "CHB-27"}))
        (Path(self.cache.name) / "CHB-20-auth.json").symlink_to(victim)

        with mock.patch.object(hook, "jira_parent", return_value="CHB-19"):
            self.assertEqual(hook.parent_epic("CHB-20-auth", "CHB-20"), "CHB-19")
        self.assertIn("CHB-27", victim.read_text())

    def test_the_default_folder_is_per_user(self) -> None:
        with mock.patch.dict(os.environ, {hook.CACHE_DIR_ENV: ""}):
            self.assertTrue(hook.cache_dir().endswith(f"chb-harness-epics-{os.getuid()}"))


class RepositoryTest(unittest.TestCase):
    """Real git: the branch and the toplevel come from the edited file's own worktree."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.main = _git_repo(Path(tmp.name) / "repo", "CHB-20-auth")
        self.worktree = _git_repo(self.main / ".claude" / "worktrees" / "agent-x", "CHB-21-other")
        self.lookup = mock.patch.object(hook, "parent_epic", return_value="CHB-19").start()
        self.addCleanup(mock.patch.stopall)

    def test_a_harness_file_on_a_product_branch_is_denied(self) -> None:
        why = hook.reason(str(self.main / "factory" / "hooks" / "new.py"), cwd=str(self.main)) or ""

        self.assertIn("CHB-20", why)
        self.assertIn("CHB-19", why)
        self.assertIn("factory", why)
        self.lookup.assert_called_once_with("CHB-20-auth", "CHB-20")

    def test_a_relative_path_resolves_against_the_working_directory(self) -> None:
        self.assertIsNotNone(hook.reason(".github/workflows/ci.yml", cwd=str(self.main)))

    def test_a_product_file_in_a_worktree_under_dot_claude_is_allowed_without_jira(self) -> None:
        self.assertIsNone(hook.reason(str(self.worktree / "backend" / "a.py"), cwd=str(self.main)))
        self.assertIsNone(hook.reason("backend/a.py", cwd=str(self.worktree)))
        self.lookup.assert_not_called()

    def test_the_worktree_branch_decides_for_its_own_harness_files(self) -> None:
        self.assertIsNotNone(hook.reason(str(self.worktree / ".claude" / "settings.json"), cwd=str(self.main)))
        self.lookup.assert_called_once_with("CHB-21-other", "CHB-21")

    def test_a_factory_ticket_passes(self) -> None:
        self.lookup.return_value = "CHB-27"

        self.assertIsNone(hook.reason(str(self.main / "factory" / "x.py"), cwd=str(self.main)))

    def test_a_ticket_without_a_parent_passes(self) -> None:
        self.lookup.return_value = None

        self.assertIsNone(hook.reason(str(self.main / "factory" / "x.py"), cwd=str(self.main)))

    def test_a_product_epic_branch_itself_is_denied(self) -> None:
        epic = _git_repo(self.main.parent / "epic", "CHB-19-security")
        self.lookup.return_value = None

        self.assertIsNotNone(hook.reason(str(epic / ".claude" / "x"), cwd=str(epic)))

    def test_branches_that_are_not_ticket_branches_pass_without_jira(self) -> None:
        for branch in ["main", "worktree-agent-abc"]:
            with self.subTest(branch):
                repo = _git_repo(self.main.parent / branch, branch)
                self.assertIsNone(hook.reason(str(repo / "factory" / "x.py"), cwd=str(repo)))
        self.lookup.assert_not_called()

    def test_a_path_outside_any_repository_passes(self) -> None:
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)

        self.assertIsNone(hook.reason(str(Path(outside.name) / ".claude" / "settings.json"), cwd="/"))
        self.lookup.assert_not_called()

    def test_jira_unreadable_denies_with_a_message(self) -> None:
        self.lookup.side_effect = hook.JiraUnreadable("no token")

        why = hook.reason(str(self.main / "factory" / "x.py"), cwd=str(self.main)) or ""

        self.assertIn("could not read", why)
        self.assertIn("no token", why)
        self.assertIn("factory/bin/jira view CHB-20", why)


def _main(stdin: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "stdin", io.StringIO(stdin)), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = hook.main()
    return code, out.getvalue(), err.getvalue()


def _event(tool: str = "Edit", **tool_input: str) -> str:
    return json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool, "cwd": "/repo", "tool_input": tool_input})


class ContractTest(unittest.TestCase):
    def test_a_denied_edit_exits_2_with_the_reason_on_stderr(self) -> None:
        with mock.patch.object(hook, "reason", return_value="Blocked: x"):
            self.assertEqual(_main(_event(file_path="/repo/factory/x.py")), (2, "", "Blocked: x\n"))

    def test_a_notebook_path_is_checked_too(self) -> None:
        with mock.patch.object(hook, "reason", return_value="Blocked: x") as reason:
            self.assertEqual(_main(_event("NotebookEdit", notebook_path="/repo/factory/x.ipynb"))[0], 2)

        reason.assert_called_once_with("/repo/factory/x.ipynb", cwd="/repo")

    def test_an_allowed_edit_exits_0_silently(self) -> None:
        self.assertEqual(_main(_event(file_path="/repo/backend/x.py")), (0, "", ""))

    def test_input_without_a_path_is_left_alone(self) -> None:
        self.assertEqual(_main(_event("Bash", command="ls"))[0], 0)
        self.assertEqual(_main("[]")[0], 0)

    def test_unreadable_input_is_denied(self) -> None:
        self.assertEqual(_main("not json")[0], 2)

    def test_an_error_in_the_matcher_denies(self) -> None:
        with mock.patch.object(hook, "reason", side_effect=ValueError("boom")):
            code, _, err = _main(_event(file_path="/repo/x"))

        self.assertEqual(code, 2)
        self.assertIn("hook failed (ValueError)", err)

    def test_project_settings_register_the_hook_on_file_edits(self) -> None:
        settings = json.loads((ROOT / ".claude" / "settings.json").read_text())
        (group,) = [g for g in settings["hooks"]["PreToolUse"] if "deny_harness_edits.py" in g["hooks"][0]["command"]]

        self.assertEqual(set(group["matcher"].split("|")), {"Write", "Edit", "MultiEdit", "NotebookEdit"})
        self.assertIn("python3 -I", group["hooks"][0]["command"])
        self.assertIn("factory/hooks/deny_harness_edits.py", group["hooks"][0]["command"])


if __name__ == "__main__":
    unittest.main()
