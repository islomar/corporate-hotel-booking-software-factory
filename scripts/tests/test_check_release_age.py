import os
import subprocess
import sys
import tempfile
import contextlib
import io
import json
import unittest
from unittest import mock
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import check_release_age
from check_release_age import (
    ConfigError,
    Violation,
    alert_fixes,
    check_lockfile,
    fetch_open_alerts,
    repository_folder,
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


DEPENDABOT_ENV = {"GH_TOKEN": "t", "GITHUB_REPOSITORY": "o/r", "PR_AUTHOR": "dependabot[bot]"}

LOCK_WITH_VITE = """{"packages": {"": {},
  "node_modules/vite": {"version": "8.3.6", "resolved": "https://registry.npmjs.org/vite/-/vite-8.3.6.tgz"}}}"""


class SecurityFixTest(unittest.TestCase):
    """A fresh fix for an open Dependabot alert passes the window (CHB-95)."""

    def test_reads_npm_fixes_for_this_manifest_only(self):
        alerts = [
            alert("vite", "8.3.6"),
            alert("vite", "9.0.1", number=8),
            alert("other", "1.0.0", manifest="e2e/package-lock.json"),
            alert("django", "5.0.1", ecosystem="pip"),
            alert("nofix", None),
            {"number": 9, "dependency": {"package": {"ecosystem": "npm"}, "manifest_path": "frontend/package-lock.json"},
             "security_vulnerability": {"first_patched_version": {"identifier": "1.0.0"}}},
            "not an alert",
        ]

        self.assertEqual(alert_fixes(alerts, "frontend"), {"vite": [("8.3.6", 7), ("9.0.1", 8)]})

    def test_exempts_only_the_first_patched_version_of_the_alerted_package(self):
        fixes = {"vite": [("8.3.6", 7)]}
        cases = {
            ("vite", "8.3.6"): True,
            ("vite", "v8.3.6"): True,
            ("vite", "8.3.7"): False,
            ("vite", "8.4.0"): False,
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

        fixes, out = self._load({"PR_AUTHOR": "dependabot[bot]"}, fetch)

        self.assertEqual(fixes, {})
        self.assertIn("no security-fix exemption", out)

    def test_an_api_error_is_a_warning_not_a_failure(self):
        def fetch(*_):
            raise OSError("HTTP 403")

        fixes, out = self._load({"GH_TOKEN": "t", "GITHUB_REPOSITORY": "o/r", "PR_AUTHOR": "dependabot[bot]"}, fetch)

        self.assertEqual(fixes, {})
        self.assertIn("::warning", out)

    def test_reads_the_alerts_for_this_folder(self):
        fixes, _ = self._load(DEPENDABOT_ENV, lambda repo, token: [alert("vite", "8.3.6")])

        self.assertEqual(fixes, {"vite": [("8.3.6", 7)]})

    def test_a_pull_request_by_anyone_else_gets_no_exemption_and_no_call(self):
        def fetch(*_):
            raise AssertionError("must not call the API")

        env = {**DEPENDABOT_ENV, "PR_AUTHOR": "chb-factory-agent[bot]"}

        self.assertEqual(self._load(env, fetch)[0], {})




class CheckLockfileTest(unittest.TestCase):
    """check_lockfile end to end, with the registry and the alerts API replaced."""

    def _check(self, alerts, age=timedelta(days=1), env=DEPENDABOT_ENV):
        calls = []

        def fetch(repo, token):
            calls.append(repo)
            return alerts

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / ".npmrc").write_text("min-release-age=7\n")
            lock = folder / "package-lock.json"
            lock.write_text(LOCK_WITH_VITE)
            with (
                mock.patch.dict("os.environ", env, clear=True),
                mock.patch("check_release_age.fetch_publish_time", return_value=NOW - age),
                mock.patch("check_release_age.fetch_open_alerts", fetch),
                mock.patch("check_release_age.repository_folder", return_value="frontend"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                return check_lockfile(lock, None, NOW), calls

    def test_a_fresh_fix_for_an_open_alert_passes_on_a_dependabot_pr(self):
        self.assertEqual(self._check([alert("vite", "8.3.6")]), ([], ["o/r"]))

    def test_the_same_entry_fails_without_an_alert(self):
        violations, _ = self._check([])

        self.assertEqual([(v.name, v.version) for v in violations], [("vite", "8.3.6")])

    def test_an_old_enough_entry_never_calls_the_api(self):
        self.assertEqual(self._check([alert("vite", "8.3.6")], age=timedelta(days=30)), ([], []))


class FakeResponse(io.BytesIO):
    def __init__(self, body, link=""):
        super().__init__(json.dumps(body).encode())
        self.headers = {"Link": link}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def next_link(page):
    return f'<https://api.github.com/repositories/1/dependabot/alerts?after={page}>; rel="next"'


class FetchOpenAlertsTest(unittest.TestCase):
    def _fetch(self, responses):
        urls = []

        def urlopen(request, timeout):
            urls.append(request.full_url)
            return responses.pop(0)

        with mock.patch("urllib.request.urlopen", urlopen):
            return fetch_open_alerts("o/r", "t"), urls

    def test_follows_the_link_header_to_the_last_page(self):
        alerts, urls = self._fetch([FakeResponse([{"number": 1}], next_link(2)), FakeResponse([{"number": 2}])])

        self.assertEqual([a["number"] for a in alerts], [1, 2])
        self.assertTrue(urls[1].endswith("after=2"))

    def test_exactly_the_page_cap_is_fine_one_more_is_an_error(self):
        cap = check_release_age.MAX_ALERT_PAGES
        pages = [FakeResponse([], next_link(n)) for n in range(cap - 1)] + [FakeResponse([])]
        self.assertEqual(self._fetch(pages)[0], [])

        pages = [FakeResponse([], next_link(n)) for n in range(cap + 1)]
        with self.assertRaises(ValueError):
            self._fetch(pages)

    def test_refuses_a_next_page_on_another_host(self):
        evil = '<https://example.com/steal>; rel="next"'
        with self.assertRaises(ValueError):
            self._fetch([FakeResponse([], evil)])

    def test_refuses_an_answer_that_is_not_a_list(self):
        with self.assertRaises(ValueError):
            self._fetch([FakeResponse({"message": "Bad credentials"})])


class RepositoryFolderTest(unittest.TestCase):
    def test_maps_a_lockfile_to_its_folder_from_the_repository_root(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True, env=env)  # noqa: S603,S607
            (root / "frontend").mkdir()
            with mock.patch.dict("os.environ", env, clear=True), contextlib.chdir(root / "frontend"):
                self.assertEqual(repository_folder(Path("package-lock.json")), "frontend")
            with mock.patch.dict("os.environ", env, clear=True), contextlib.chdir(root):
                self.assertEqual(repository_folder(Path("package-lock.json")), ".")


if __name__ == "__main__":
    unittest.main()
