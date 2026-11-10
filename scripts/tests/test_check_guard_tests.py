import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from check_guard_tests import ROOT, TF_DIR, main, scrubbed_env


def done(returncode=0, stdout=""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout)


class Fakes:
    def __init__(self, version="1.17.1", failing=None):
        self.version = version
        self.failing = failing
        self.commands = []
        self.envs = []

    def capture(self, command, cwd, env):
        if self.version is None:
            raise FileNotFoundError("terraform")
        return done(stdout=json.dumps({"terraform_version": self.version}))

    def run(self, command, cwd, env):
        self.commands.append((list(command), cwd))
        self.envs.append(dict(env))
        return done(1 if self.failing and self.failing in command else 0)


class MainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / TF_DIR).mkdir(parents=True)
        (self.root / TF_DIR / ".terraform-version").write_text("1.17.1\n")

    def call(self, fakes, environ=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(self.root, environ or {}, fakes.run, fakes.capture)
        return code, out.getvalue()

    def test_runs_init_then_test_in_the_terraform_folder(self):
        fakes = Fakes()
        code, _ = self.call(fakes)
        self.assertEqual(code, 0)
        self.assertEqual(
            [c for c, _ in fakes.commands],
            [
                ["terraform", "init", "-backend=false", "-input=false", "-no-color"],
                ["terraform", "test", "-no-color"],
            ],
        )
        self.assertEqual({cwd for _, cwd in fakes.commands}, {self.root / TF_DIR})

    def test_failing_test_fails_the_check(self):
        code, out = self.call(Fakes(failing="test"))
        self.assertEqual(code, 1)
        self.assertIn("guard", out)

    def test_failing_init_stops_before_the_test(self):
        fakes = Fakes(failing="init")
        code, _ = self.call(fakes)
        self.assertEqual(code, 1)
        self.assertEqual(len(fakes.commands), 1)

    def test_wrong_terraform_version_fails_without_running(self):
        fakes = Fakes(version="1.9.0")
        code, out = self.call(fakes)
        self.assertEqual(code, 1)
        self.assertEqual(fakes.commands, [])
        self.assertIn("1.17.1", out)
        self.assertIn("1.9.0", out)

    def test_missing_terraform_fails(self):
        code, out = self.call(Fakes(version=None))
        self.assertEqual(code, 1)
        self.assertIn("found none", out)

    def test_credentials_never_reach_terraform(self):
        fakes = Fakes()
        environ = {"GITHUB_TOKEN": "x", "GOOGLE_OAUTH_ACCESS_TOKEN": "y", "TF_VAR_alert_email": "z", "PATH": "/bin"}
        self.call(fakes, environ)
        env = fakes.envs[0]
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("GOOGLE_OAUTH_ACCESS_TOKEN", env)
        self.assertNotIn("TF_VAR_alert_email", env)
        self.assertEqual(env["PATH"], "/bin")

    def test_scratch_data_dir_is_outside_the_checkout(self):
        self.assertEqual(scrubbed_env({}, "/scratch")["TF_DATA_DIR"], "/scratch")
        fakes = Fakes()
        self.call(fakes)
        self.assertFalse(fakes.envs[0]["TF_DATA_DIR"].startswith(str(self.root)))


class RepositoryTest(unittest.TestCase):
    def test_the_real_pin_and_test_file_exist(self):
        self.assertTrue((ROOT / TF_DIR / ".terraform-version").is_file())
        self.assertTrue((ROOT / TF_DIR / "tests" / "guards.tftest.hcl").is_file())


if __name__ == "__main__":
    unittest.main()
