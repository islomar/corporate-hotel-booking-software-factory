import base64
import io
import json
import os
import subprocess
import sys
import unittest
import urllib.error
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import as_agent
from as_agent import (
    EXIT_NO_AUTO_MERGE,
    REPOSITORY_CANDIDATES,
    KEY_COMMAND,
    AppKeyError,
    agent_env,
    installation_token,
    jwt_signing_input,
    noreply_email,
    read_private_key,
    read_private_key_with_override,
    run,
)
from op_service_account import ServiceAccountError


def _decode(part: str) -> dict[str, object]:
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


class JwtSigningInputTest(unittest.TestCase):
    def test_header_and_claims_for_a_github_app(self) -> None:
        header, claims = jwt_signing_input("Iv23client", now=1_000_000).split(".")

        self.assertEqual(_decode(header), {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(_decode(claims), {"iat": 999_940, "exp": 1_000_540, "iss": "Iv23client"})

    def test_parts_are_unpadded_base64url(self) -> None:
        self.assertNotIn("=", jwt_signing_input("Iv23client", now=1_000_000))


class AgentEnvTest(unittest.TestCase):
    def setUp(self) -> None:
        self.env = agent_env(
            {"PATH": "/bin", "GH_TOKEN": "human-token", "GITHUB_TOKEN": "human", "OP_SERVICE_ACCOUNT_TOKEN": "ops_x"},
            token="ghs_agent",
            bot_login="chb-factory-agent[bot]",
            bot_id=42,
        )

    def test_gh_uses_the_installation_token(self) -> None:
        self.assertEqual(self.env["GH_TOKEN"], "ghs_agent")
        self.assertNotIn("GITHUB_TOKEN", self.env)
        self.assertNotIn("OP_SERVICE_ACCOUNT_TOKEN", self.env)
        self.assertEqual(self.env["PATH"], "/bin")

    def test_commits_are_authored_by_the_bot(self) -> None:
        email = "42+chb-factory-agent[bot]@users.noreply.github.com"
        for role in ("AUTHOR", "COMMITTER"):
            self.assertEqual(self.env[f"GIT_{role}_NAME"], "chb-factory-agent[bot]")
            self.assertEqual(self.env[f"GIT_{role}_EMAIL"], email)

    def test_git_pushes_over_https_with_the_token(self) -> None:
        count = int(self.env["GIT_CONFIG_COUNT"])
        config = {self.env[f"GIT_CONFIG_KEY_{i}"]: self.env[f"GIT_CONFIG_VALUE_{i}"] for i in range(count)}

        self.assertEqual(config["url.https://github.com/.insteadOf"], "git@github.com:")
        self.assertEqual(config["credential.https://github.com.helper"], "!gh auth git-credential")


def _runner(returncode: int = 0, stdout: bytes = b"-----BEGIN KEY-----\n") -> Any:
    calls: list[dict[str, Any]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append({"command": command, **kwargs})
        return subprocess.CompletedProcess(command, returncode, stdout, b"")

    run.calls = calls  # type: ignore[attr-defined]
    return run


class ReadPrivateKeyTest(unittest.TestCase):
    def test_the_default_command_reads_the_document_from_the_factory_vault(self) -> None:
        self.assertEqual(
            KEY_COMMAND, "op document get chb-factory-agent.2026-11-06.private-key --vault chb-factory"
        )

    def test_op_gets_the_service_account_token_in_its_own_environment_only(self) -> None:
        base = {"PATH": "/bin"}
        run = _runner()

        key = read_private_key(["op", "document", "get", "k"], base, token_reader=lambda: "ops_fake", run=run)

        self.assertEqual(key, b"-----BEGIN KEY-----\n")
        (call,) = run.calls
        self.assertEqual(call["env"], {"PATH": "/bin", "OP_SERVICE_ACCOUNT_TOKEN": "ops_fake"})
        self.assertEqual(base, {"PATH": "/bin"})
        self.assertGreater(call["timeout"], 0)

    def test_no_token_in_the_keyring_stops_before_op(self) -> None:
        def no_token() -> str:
            raise ServiceAccountError("no 1Password service account token in the keyring")

        run = _runner()
        with self.assertRaisesRegex(AppKeyError, "keyring"):
            read_private_key(["op"], {}, token_reader=no_token, run=run)
        self.assertEqual(run.calls, [])

    def test_failing_op_or_empty_output_is_an_error_without_output(self) -> None:
        for run in (_runner(returncode=1, stdout=b"secret"), _runner(stdout=b" \n")):
            with self.assertRaises(AppKeyError) as caught:
                read_private_key(["op"], {}, token_reader=lambda: "ops_fake", run=run)
            self.assertNotIn("secret", str(caught.exception))

    def test_missing_op_is_an_error(self) -> None:
        with self.assertRaises(AppKeyError):
            read_private_key(["/nonexistent/op"], {}, token_reader=lambda: "ops_fake")


class KeyCommandOverrideTest(unittest.TestCase):
    def test_the_override_never_gets_the_service_account_token(self) -> None:
        run = _runner()

        key = read_private_key_with_override("my-key --name k", {"PATH": "/bin", "OP_SERVICE_ACCOUNT_TOKEN": "ops_x"}, run)

        self.assertEqual(key, b"-----BEGIN KEY-----\n")
        (call,) = run.calls
        self.assertEqual(call["command"], ["my-key", "--name", "k"])
        self.assertEqual(call["env"], {"PATH": "/bin"})

    def test_a_failing_override_is_an_error_without_output(self) -> None:
        for run in (_runner(returncode=1, stdout=b"secret"), _runner(stdout=b"")):
            with self.assertRaises(AppKeyError) as caught:
                read_private_key_with_override("my-key", {}, run)
            self.assertNotIn("secret", str(caught.exception))


URL = "https://github.com/islomar/corporate-hotel-booking-software-factory/pull/61"


def _gh(create_code: int = 0, create_out: str = f"Creating pull request\n{URL}\n", merge_code: int = 0) -> Any:
    """A fake subprocess.run: `gh pr create` prints create_out, `gh pr merge` exits merge_code."""
    calls: list[dict[str, Any]] = []

    def fake(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append({"command": command, **kwargs})
        if command[1:3] == ["pr", "merge"]:
            return subprocess.CompletedProcess(command, merge_code, None, None)
        captured = kwargs.get("stdout") == subprocess.PIPE
        return subprocess.CompletedProcess(command, create_code, create_out if captured else None, None)

    fake.calls = calls  # type: ignore[attr-defined]
    return fake


class AutoMergeTest(unittest.TestCase):
    env = {"GH_TOKEN": "ghs_agent"}

    def _run(self, argv: list[str], fake: Any, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = run(argv, env or self.env, runner=fake, out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def test_a_new_pr_gets_auto_merge_squash_with_the_same_token(self) -> None:
        fake = _gh()

        code, out, err = self._run(["gh", "pr", "create", "--title", "t", "--body", "b"], fake)

        self.assertEqual(code, 0)
        self.assertIn(URL, out)
        self.assertIn("auto-merge (squash) enabled", err)
        create, merge = fake.calls
        self.assertEqual(merge["command"], ["gh", "pr", "merge", "--auto", "--squash", URL])
        self.assertEqual(merge["env"], self.env)
        self.assertEqual(create["env"], self.env)

    def test_gh_pr_new_is_the_same_command(self) -> None:
        fake = _gh()

        self.assertEqual(self._run(["/usr/bin/gh", "pr", "new", "--fill"], fake)[0], 0)
        self.assertEqual(len(fake.calls), 2)

    def test_base_main_gets_auto_merge(self) -> None:
        for base in (["--base", "main"], ["-B", "main"], ["--base=main"]):
            fake = _gh()
            self._run(["gh", "pr", "create", "--fill", *base], fake)
            self.assertEqual(len(fake.calls), 2, base)

    def test_skipped_for_opt_out_draft_stacked_pr_and_web(self) -> None:
        cases = [
            ("AGENT_NO_AUTO_MERGE", ["--fill"], {"AGENT_NO_AUTO_MERGE": "1"}),
            ("draft", ["--fill", "--draft"], {}),
            ("draft", ["--fill", "-d"], {}),
            ("not main", ["--fill", "--base", "CHB-1-parent"], {}),
            ("not main", ["--fill", "--base=CHB-1-parent"], {}),
            ("command line", ["--web"], {}),
        ]
        for reason, args, extra in cases:
            fake = _gh()
            code, _, err = self._run(["gh", "pr", "create", *args], fake, {**self.env, **extra})
            self.assertEqual(code, 0, reason)
            self.assertIn(reason, err)
            (call,) = fake.calls
            self.assertNotIn("stdout", call, "stdout is not captured when auto-merge is skipped")

    def test_a_failed_create_returns_its_exit_code_and_merges_nothing(self) -> None:
        fake = _gh(create_code=1, create_out="")

        code, _, _ = self._run(["gh", "pr", "create", "--fill"], fake)

        self.assertEqual(code, 1)
        self.assertEqual(len(fake.calls), 1)

    def test_no_pr_url_in_the_output_is_a_distinct_exit_code(self) -> None:
        fake = _gh(create_out="done\n")

        code, _, err = self._run(["gh", "pr", "create", "--fill"], fake)

        self.assertEqual(code, EXIT_NO_AUTO_MERGE)
        self.assertEqual(len(fake.calls), 1)
        self.assertIn("no PR URL", err)

    def test_a_failed_merge_says_the_pr_is_open_and_how_to_retry(self) -> None:
        fake = _gh(merge_code=1)

        code, out, err = self._run(["gh", "pr", "create", "--fill"], fake)

        self.assertEqual(code, EXIT_NO_AUTO_MERGE)
        self.assertIn(URL, out)
        self.assertIn("Do not open it again", err)
        self.assertIn(f"factory/bin/as-agent gh pr merge --auto --squash {URL}", err)

    def test_other_commands_pass_through_without_capturing_stdout(self) -> None:
        for argv in (["git", "push"], ["gh", "pr", "checks", "61", "--watch"], ["gh", "pr", "merge", "61"]):
            fake = _runner(returncode=7)
            code, out, err = self._run(argv, fake)
            self.assertEqual(code, 7)
            self.assertEqual((out, err), ("", ""))
            (call,) = fake.calls
            self.assertEqual(call["command"], argv)
            self.assertNotIn("stdout", call)


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.github.com/x", code, "error", {}, None)  # type: ignore[arg-type]


def _api(answers: dict[str, object]) -> Any:
    """A fake as_agent._api: each path answers a dict, or raises the HTTPError it maps to."""
    calls: list[tuple[str, str, object]] = []

    def api(method: str, path: str, auth: str, body: object | None = None) -> object:
        calls.append((method, path, body))
        answer = answers[path]
        if isinstance(answer, Exception):
            raise answer
        return answer

    api.calls = calls  # type: ignore[attr-defined]
    return api


class InstallationTokenTest(unittest.TestCase):
    """CHB-218: the App installation is found under the organization name, then the personal one."""

    NEW, OLD = REPOSITORY_CANDIDATES

    def _token(self, api: Any) -> str:
        with mock.patch.object(as_agent, "_sign", lambda signing_input, key: "jwt"):
            return installation_token("Iv23client", b"key", api=api)

    def test_a_404_on_the_first_name_tries_the_next(self) -> None:
        api = _api(
            {
                f"/repos/{self.NEW}/installation": _http_error(404),
                f"/repos/{self.OLD}/installation": {"id": 7},
                "/app/installations/7/access_tokens": {"token": "ghs_x"},
            }
        )

        self.assertEqual(self._token(api), "ghs_x")
        self.assertEqual(
            api.calls,
            [
                ("GET", f"/repos/{self.NEW}/installation", None),
                ("GET", f"/repos/{self.OLD}/installation", None),
                ("POST", "/app/installations/7/access_tokens", {"repositories": ["corporate-hotel-booking-software-factory"]}),
            ],
        )

    def test_the_first_name_that_answers_200_wins(self) -> None:
        api = _api({f"/repos/{self.NEW}/installation": {"id": 9}, "/app/installations/9/access_tokens": {"token": "ghs_y"}})

        self.assertEqual(self._token(api), "ghs_y")
        self.assertEqual([path for _, path, _ in api.calls], [f"/repos/{self.NEW}/installation", "/app/installations/9/access_tokens"])

    def test_an_error_other_than_404_stops(self) -> None:
        api = _api({f"/repos/{self.NEW}/installation": _http_error(401)})

        with self.assertRaises(urllib.error.HTTPError):
            self._token(api)
        self.assertEqual(len(api.calls), 1)

    def test_no_installation_under_any_name_names_them_all(self) -> None:
        api = _api({f"/repos/{name}/installation": _http_error(404) for name in REPOSITORY_CANDIDATES})

        with self.assertRaisesRegex(as_agent.NoInstallationError, "islomar-software-garden/.*islomar/"):
            self._token(api)

    def test_main_stops_with_guidance_when_no_installation_answers(self) -> None:
        error = as_agent.NoInstallationError("the App chb-factory-agent is not installed on a or b")
        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"AGENT_APP_KEY_COMMAND": "true"}),
            mock.patch.object(as_agent, "read_private_key_with_override", lambda command, env: b"key"),
            mock.patch.object(as_agent, "installation_token", mock.Mock(side_effect=error)),
            mock.patch.object(as_agent, "run", mock.Mock(side_effect=AssertionError("must not run"))),
            mock.patch("sys.stderr", stderr),
        ):
            code = as_agent.main(["gh", "pr", "list"])

        self.assertEqual(code, as_agent.EXIT_NO_INSTALLATION)
        self.assertIn("not installed on a or b", stderr.getvalue())
        self.assertIn("STOP: do not use another identity", stderr.getvalue())


class NoreplyEmailTest(unittest.TestCase):
    def test_github_noreply_format(self) -> None:
        self.assertEqual(noreply_email("bot[bot]", 7), "7+bot[bot]@users.noreply.github.com")



class ScratchFolderTest(unittest.TestCase):
    """CHB-138: parallel agents overwrote each other's pr.md in one shared scratch folder."""

    SHARED = "/tmp/claude-1000/project/session/scratchpad/pr.md"
    OWN = "/tmp/claude-1000/project/session/scratchpad/CHB-138/pr.md"

    def _err(self, argv: list[str]) -> tuple[int, str]:
        fake = _runner(returncode=0)
        err = io.StringIO()
        code = run(argv, {}, runner=fake, out=io.StringIO(), err=err)
        self.assertEqual(fake.calls[0]["command"], argv, "the warning never blocks the command")
        return code, err.getvalue()

    def test_a_file_in_a_shared_scratch_folder_gets_a_warning(self) -> None:
        for argv in (
            ["git", "commit", "-F", self.SHARED],
            ["gh", "pr", "comment", "61", "--body-file", self.SHARED],
            ["gh", "pr", "edit", "61", f"--body-file={self.SHARED}"],
            ["gh", "issue", "create", f"--file={self.SHARED}"],
            ["gh", "api", "repos/o/r/issues/1/comments", "-F", f"body=@{self.SHARED}"],
        ):
            code, err = self._err(argv)
            self.assertEqual(code, 0)
            self.assertIn(f"as-agent: warning: {self.SHARED} is in a shared scratch folder", err, argv)
            self.assertIn("<scratchpad>/CHB-<n>/", err)

    def test_a_file_in_a_ticket_subfolder_or_outside_the_scratchpad_gets_none(self) -> None:
        for path in (self.OWN, "/home/me/repo/pr.md", "-"):
            self.assertEqual(self._err(["git", "commit", "-F", path])[1], "", path)

    def test_a_trailing_option_without_a_value_gets_none(self) -> None:
        self.assertEqual(self._err(["git", "commit", "-F"])[1], "")


if __name__ == "__main__":
    unittest.main()
