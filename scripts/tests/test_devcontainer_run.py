"""Tests of .devcontainer/run, the launcher that runs one command in the sandbox container (CHB-31)."""

import importlib.machinery
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / ".devcontainer" / "run"


def _load():
    loader = importlib.machinery.SourceFileLoader("devcontainer_run", str(LAUNCHER))
    spec = importlib.util.spec_from_loader("devcontainer_run", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["devcontainer_run"] = module
    loader.exec_module(module)
    return module


run = _load()

TOP = "/home/u/repo/.claude/worktrees/CHB-9"
COMMON = "/home/u/repo/.git"


class DockerArgsTest(unittest.TestCase):
    def args(self, command=("make", "check"), tty=False):
        return run.docker_args(TOP, COMMON, "chb-sandbox:abc", list(command), tty=tty)

    def mounts(self, args):
        return [args[i + 1] for i, word in enumerate(args) if word == "--mount"]

    def test_the_checkout_is_mounted_at_its_own_path_so_git_worktree_links_resolve(self):
        args = self.args()
        self.assertIn(f"type=bind,source={TOP},target={TOP}", self.mounts(args))
        self.assertEqual(args[args.index("--workdir") + 1], TOP)

    def test_git_and_claude_settings_are_read_only_so_the_container_cannot_plant_code_the_host_runs(self):
        mounts = self.mounts(self.args())
        self.assertIn(f"type=bind,source={COMMON},target={COMMON},readonly", mounts)
        self.assertIn(f"type=bind,source={TOP}/.claude,target={TOP}/.claude,readonly", mounts)

    def test_node_modules_live_in_a_volume_of_this_checkout_not_in_the_host_folder(self):
        mounts = self.mounts(self.args())
        volume = [m for m in mounts if m.endswith(f"target={TOP}/frontend/node_modules")]
        self.assertEqual(len(volume), 1)
        self.assertTrue(volume[0].startswith("type=volume,source=chb-sandbox-nm-"))
        other = run.docker_args("/elsewhere", COMMON, "img", ["true"], tty=False)
        self.assertNotIn(volume[0].split(",")[1], " ".join(other))

    def test_no_host_environment_or_secret_reaches_the_container(self):
        args = self.args()
        self.assertNotIn("--env", args)
        self.assertNotIn("-e", args)
        self.assertNotIn("--env-file", args)

    def test_only_the_capabilities_the_firewall_needs(self):
        args = self.args()
        caps = [args[i + 1] for i, word in enumerate(args) if word == "--cap-add"]
        self.assertEqual(sorted(caps), ["NET_ADMIN", "NET_RAW"])
        self.assertNotIn("--privileged", args)

    def test_the_command_follows_the_image_unchanged(self):
        args = self.args(command=("bash", "-c", "npm ci && make check"))
        self.assertEqual(args[-4:], ["chb-sandbox:abc", "bash", "-c", "npm ci && make check"])

    def test_a_terminal_gets_a_tty(self):
        self.assertIn("-t", self.args(tty=True))
        self.assertNotIn("-t", self.args(tty=False))


class ImageTagTest(unittest.TestCase):
    def test_the_tag_changes_when_a_file_of_the_image_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            context = Path(folder)
            for name in run.IMAGE_FILES:
                (context / name).write_text(name)
            first = run.image_tag(context)
            (context / "allowed-hosts.txt").write_text("pypi.org\n")
            self.assertNotEqual(run.image_tag(context), first)
            self.assertTrue(first.startswith("chb-sandbox:"))


if __name__ == "__main__":
    unittest.main()
