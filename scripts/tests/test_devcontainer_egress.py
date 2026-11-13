import contextlib
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".devcontainer"))

from egress import ALLOW_LIST, check, main, parse_allowlist


def reaches(hosts: set[str]):
    """A connect function that succeeds only for the given hosts."""

    def connect(host: str) -> bool:
        return host in hosts

    return connect


def committed_hosts() -> list[str]:
    return parse_allowlist(ALLOW_LIST.read_text(encoding="utf-8"))


class ParseAllowlistTest(unittest.TestCase):
    def test_skips_comments_and_blank_lines(self):
        text = "# registries\n\nregistry.npmjs.org\n  pypi.org  # python\n"
        self.assertEqual(parse_allowlist(text), ["registry.npmjs.org", "pypi.org"])

    def test_lowercases_hosts(self):
        self.assertEqual(parse_allowlist("PyPI.org\n"), ["pypi.org"])

    def test_rejects_a_scheme(self):
        with self.assertRaisesRegex(ValueError, "line 1"):
            parse_allowlist("https://pypi.org\n")

    def test_rejects_a_path_or_port(self):
        for bad in ("pypi.org/simple", "pypi.org:443"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_allowlist(bad + "\n")

    def test_rejects_a_wildcard(self):
        with self.assertRaisesRegex(ValueError, "line 2"):
            parse_allowlist("pypi.org\n*.googleapis.com\n")

    def test_rejects_a_bare_ip(self):
        with self.assertRaises(ValueError):
            parse_allowlist("1.2.3.4\n")

    def test_rejects_a_duplicate(self):
        with self.assertRaisesRegex(ValueError, "line 2"):
            parse_allowlist("pypi.org\npypi.org\n")

    def test_rejects_an_empty_list(self):
        with self.assertRaises(ValueError):
            parse_allowlist("# nothing\n")


class CheckTest(unittest.TestCase):
    allowed = ["pypi.org", "github.com"]

    def run_check(self, reachable: set[str], allowed_host="pypi.org", blocked_host="example.com"):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = check(allowed_host, blocked_host, self.allowed, reaches(reachable))
        return code, out.getvalue()

    def test_passes_when_allowed_connects_and_blocked_does_not(self):
        code, _ = self.run_check({"pypi.org"})
        self.assertEqual(code, 0)

    def test_fails_when_the_blocked_host_connects(self):
        code, out = self.run_check({"pypi.org", "example.com"})
        self.assertEqual(code, 1)
        self.assertIn("example.com", out)

    def test_fails_when_the_allowed_host_does_not_connect(self):
        code, _ = self.run_check(set())
        self.assertEqual(code, 2)

    def test_blocked_connection_wins_over_allowed_failure(self):
        code, _ = self.run_check({"example.com"})
        self.assertEqual(code, 1)

    def test_fails_when_the_blocked_host_is_on_the_allow_list(self):
        code, _ = self.run_check(set(), blocked_host="github.com")
        self.assertEqual(code, 3)

    def test_fails_when_the_allowed_host_is_not_on_the_allow_list(self):
        code, _ = self.run_check({"other.org"}, allowed_host="other.org")
        self.assertEqual(code, 3)


class MainTest(unittest.TestCase):
    def test_the_committed_allow_list_has_the_hosts_the_checks_need(self):
        hosts = committed_hosts()
        for needed in (
            "registry.npmjs.org",
            "pypi.org",
            "files.pythonhosted.org",
            "registry.terraform.io",
            "github.com",
            "api.github.com",
        ):
            self.assertIn(needed, hosts)

    def test_services_that_need_a_secret_are_not_reachable_the_container_holds_none(self):
        hosts = committed_hosts()
        for absent in ("api.anthropic.com", "islomar.atlassian.net", "my.1password.com", "oauth2.googleapis.com"):
            self.assertNotIn(absent, hosts)

    def test_hosts_command_prints_one_host_per_line(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["hosts"])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().split(), committed_hosts())

    def test_check_command_exits_non_zero_when_the_blocked_host_connects(self):
        with contextlib.redirect_stdout(io.StringIO()):
            code = main(["check"], connect=lambda host: True)
        self.assertNotEqual(code, 0)

    def test_check_command_exits_zero_when_only_listed_hosts_connect(self):
        listed = set(committed_hosts())
        with contextlib.redirect_stdout(io.StringIO()):
            code = main(["check"], connect=lambda host: host in listed)
        self.assertEqual(code, 0)

    def test_unknown_command_exits_with_usage(self):
        with contextlib.redirect_stderr(io.StringIO()):
            code = main(["nope"])
        self.assertEqual(code, 64)


if __name__ == "__main__":
    unittest.main()
