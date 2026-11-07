import sys
import tempfile
import contextlib
import io
import unittest
from unittest import mock
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from check_release_age import (
    ConfigError,
    Violation,
    alert_fixes,
    load_fixes,
    security_fix_exemptions,
    check_npm_entries,
    check_uv_lock,
    npm_registry_entries,
    parse_iso_days,
    read_npmrc,
)

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)

UV_LOCK = """
version = 1

[options]
exclude-newer-span = "P7D"

[[package]]
name = "old-pkg"
version = "1.0.0"
sdist = { url = "https://x/old.tar.gz", upload-time = "2026-10-01T00:00:00Z" }

[[package]]
name = "Fresh_Pkg"
version = "2.0.0"
sdist = { url = "https://x/fresh.tar.gz", upload-time = "2026-10-01T00:00:00Z" }
wheels = [{ url = "https://x/fresh.whl", upload-time = "2026-11-05T00:00:00Z" }]

[[package]]
name = "chb"
version = "0.1.0"
source = { editable = "." }
"""

PACKAGE_LOCK = """
{
  "packages": {
    "": {"name": "app"},
    "node_modules/vite": {"version": "8.3.6", "resolved": "https://registry.npmjs.org/vite/-/vite-8.3.6.tgz"},
    "node_modules/@types/node": {"version": "24.19.2", "resolved": "https://registry.npmjs.org/@types/node/-/node-24.19.2.tgz"},
    "node_modules/a/node_modules/b": {"version": "1.0.0", "resolved": "https://registry.npmjs.org/b/-/b-1.0.0.tgz"},
    "node_modules/local": {"version": "1.0.0", "resolved": "file:../local"},
    "node_modules/linked": {"link": true, "resolved": "packages/linked"}
  }
}
"""


class ParseIsoDaysTest(unittest.TestCase):
    def test_days_and_hours(self):
        self.assertEqual(parse_iso_days("P7D"), timedelta(days=7))
        self.assertEqual(parse_iso_days("PT24H"), timedelta(hours=24))

    def test_rejects_calendar_units(self):
        with self.assertRaises(ConfigError):
            parse_iso_days("P1M")


class UvLockTest(unittest.TestCase):
    def test_flags_a_file_uploaded_inside_the_window(self):
        violations = check_uv_lock(UV_LOCK, exempt=set(), now=NOW)
        self.assertEqual([(v.name, v.version) for v in violations], [("Fresh_Pkg", "2.0.0")])

    def test_exempt_names_are_normalized(self):
        self.assertEqual(check_uv_lock(UV_LOCK, exempt={"fresh-pkg"}, now=NOW), [])

    def test_lock_without_span_is_a_config_error(self):
        with self.assertRaises(ConfigError):
            check_uv_lock(UV_LOCK.replace('exclude-newer-span = "P7D"', ""), exempt=set(), now=NOW)


class NpmTest(unittest.TestCase):
    def test_registry_entries_skip_local_and_linked_packages(self):
        self.assertEqual(
            npm_registry_entries(PACKAGE_LOCK),
            {("vite", "8.3.6"), ("@types/node", "24.19.2"), ("b", "1.0.0")},
        )

    def test_flags_new_entries_and_honours_exclusions(self):
        published = {
            ("vite", "8.3.6"): NOW - timedelta(days=1),
            ("b", "1.0.0"): NOW - timedelta(days=30),
            ("@types/node", "24.19.2"): NOW - timedelta(days=2),
        }
        violations = check_npm_entries(
            set(published),
            window=timedelta(days=7),
            exclude=["@types/*"],
            now=NOW,
            publish_time=lambda name, version: published[(name, version)],
        )
        self.assertEqual([(v.name, v.version) for v in violations], [("vite", "8.3.6")])

    def test_npmrc_requires_a_number_of_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            npmrc = Path(tmp) / ".npmrc"
            npmrc.write_text("min-release-age=7d\n")
            with self.assertRaises(ConfigError):
                read_npmrc(npmrc)
            npmrc.write_text("# comment\nmin-release-age=7\nmin-release-age-exclude[]=@scope/*\n")
            self.assertEqual(read_npmrc(npmrc), (timedelta(days=7), ["@scope/*"]))


def alert(name, patched, manifest="frontend/package-lock.json", ecosystem="npm", number=7):
    return {
        "number": number,
        "dependency": {"package": {"ecosystem": ecosystem, "name": name}, "manifest_path": manifest},
        "security_vulnerability": {"first_patched_version": {"identifier": patched} if patched else None},
    }


def young(name, version):
    return Violation(name, version, NOW - timedelta(days=1))


class SecurityFixTest(unittest.TestCase):
    """A fresh fix for an open Dependabot alert passes the window (CHB-95)."""

    def test_reads_npm_fixes_for_this_manifest_only(self):
        alerts = [
            alert("vite", "8.3.6"),
            alert("vite", "9.0.1", number=8),
            alert("other", "1.0.0", manifest="e2e/package-lock.json"),
            alert("django", "5.0.1", ecosystem="pip"),
            alert("nofix", None),
        ]

        self.assertEqual(alert_fixes(alerts, "frontend"), {"vite": [("8.3.6", 7), ("9.0.1", 8)]})

    def test_exempts_the_alerted_package_at_or_above_the_fix_in_the_same_major(self):
        fixes = {"vite": [("8.3.6", 7)]}
        cases = {
            ("vite", "8.3.6"): True,
            ("vite", "8.4.0"): True,
            ("vite", "8.3.5"): False,
            ("vite", "9.0.0"): False,
            ("vite", "8.3.7-beta.1"): False,
            ("rollup", "4.0.0"): False,
        }
        for (name, version), exempt in cases.items():
            kept, notes = security_fix_exemptions([young(name, version)], fixes)
            self.assertEqual(kept == [], exempt, (name, version))
            self.assertEqual(bool(notes), exempt, (name, version))

    def test_note_names_the_alert(self):
        _, notes = security_fix_exemptions([young("vite", "8.3.6")], {"vite": [("8.3.6", 7)]})

        self.assertIn("vite@8.3.6", notes[0])
        self.assertIn("Dependabot alert #7", notes[0])


if __name__ == "__main__":
    unittest.main()


class LoadFixesTest(unittest.TestCase):
    """Reading alerts never fails the check: without them there is simply no exemption."""

    def _load(self, env, fetch):
        out = io.StringIO()
        with (
            mock.patch.dict("os.environ", env, clear=True),
            mock.patch("check_release_age.repository_folder", return_value="frontend"),
            contextlib.redirect_stdout(out),
        ):
            return load_fixes(Path("package-lock.json"), fetch=fetch), out.getvalue()

    def test_without_token_there_is_no_exemption_and_no_call(self):
        def fetch(*_):
            raise AssertionError("must not call the API")

        fixes, out = self._load({}, fetch)

        self.assertEqual(fixes, {})
        self.assertIn("no security-fix exemption", out)

    def test_an_api_error_is_a_warning_not_a_failure(self):
        def fetch(*_):
            raise OSError("HTTP 403")

        fixes, out = self._load({"GH_TOKEN": "t", "GITHUB_REPOSITORY": "o/r"}, fetch)

        self.assertEqual(fixes, {})
        self.assertIn("::warning", out)

    def test_reads_the_alerts_for_this_folder(self):
        fixes, _ = self._load({"GH_TOKEN": "t", "GITHUB_REPOSITORY": "o/r"}, lambda repo, token: [alert("vite", "8.3.6")])

        self.assertEqual(fixes, {"vite": [("8.3.6", 7)]})
