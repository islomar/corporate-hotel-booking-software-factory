import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from check_guard_tests import ROOT, TF_DIR, TF_DIRS, main, scrubbed_env, stray_planner_references


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
        for tf_dir in TF_DIRS:
            (self.root / tf_dir).mkdir(parents=True, exist_ok=True)
            (self.root / tf_dir / ".terraform-version").write_text("1.17.1\n")

    def call(self, fakes, environ=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(self.root, environ or {}, fakes.run, fakes.capture)
        return code, out.getvalue()

    def test_runs_init_then_test_in_every_terraform_folder(self):
        fakes = Fakes()
        code, _ = self.call(fakes)
        self.assertEqual(code, 0)
        init = ["terraform", "init", "-backend=false", "-input=false", "-no-color"]
        test = ["terraform", "test", "-no-color"]
        self.assertEqual([c for c, _ in fakes.commands], [init, test, init, test])
        self.assertEqual([cwd for _, cwd in fakes.commands], [self.root / d for d in TF_DIRS for _ in range(2)])

    def test_failing_test_fails_the_check(self):
        code, out = self.call(Fakes(failing="test"))
        self.assertEqual(code, 1)
        self.assertIn("guard", out)

    def test_failing_init_stops_before_the_test(self):
        fakes = Fakes(failing="init")
        code, _ = self.call(fakes)
        self.assertEqual(code, 1)
        self.assertEqual(len(fakes.commands), 1)

    def test_a_planner_reference_outside_the_planner_files_fails_without_running(self):
        gcp = self.root / "infra/gcp"
        (gcp / "planner.tf").write_text("resource \"google_service_account\" \"planner\" {}\n")
        (gcp / "extra.tf").write_text("member = google_service_account.planner.member\n")
        fakes = Fakes()
        code, out = self.call(fakes)
        self.assertEqual(code, 1)
        self.assertEqual(fakes.commands, [])
        self.assertIn("infra/gcp/extra.tf", out)
        self.assertNotIn("planner.tf,", out)
        self.assertEqual(stray_planner_references(self.root), ["infra/gcp/extra.tf"])

    def test_the_planner_files_may_name_the_planner(self):
        gcp = self.root / "infra/gcp"
        (gcp / "planner.tf").write_text("member = google_service_account.planner.member\n")
        (gcp / "guards.tf").write_text("x = google_iam_workload_identity_pool.github_pr\n")
        self.assertEqual(stray_planner_references(self.root), [])

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
        for tf_dir in TF_DIRS:
            self.assertTrue((ROOT / tf_dir / "tests" / "guards.tftest.hcl").is_file())

    def test_the_real_repository_has_no_stray_planner_reference(self):
        self.assertEqual(stray_planner_references(ROOT), [])


if __name__ == "__main__":
    unittest.main()
