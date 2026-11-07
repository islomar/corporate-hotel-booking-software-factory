import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from op_service_account import KEYRING_LOOKUP, ServiceAccountError, op_env, read_token, run_op

FAKE_TOKEN = "ops_fake-token-for-tests"


def _runner(returncode: int = 0, stdout: bytes = b"", error: BaseException | None = None) -> Any:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((command, kwargs))
        if error is not None:
            raise error
        return subprocess.CompletedProcess(command, returncode, stdout, b"")

    run.calls = calls  # type: ignore[attr-defined]
    return run


class ReadTokenTest(unittest.TestCase):
    def test_reads_the_token_from_the_keyring_item_of_the_factory(self) -> None:
        run = _runner(stdout=FAKE_TOKEN.encode() + b"\n")

        self.assertEqual(read_token(run), FAKE_TOKEN)
        (command, kwargs), = run.calls
        self.assertEqual(command, ["secret-tool", "lookup", "service", "chb-factory", "key", "op-service-account-token"])
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(kwargs["capture_output"])
        self.assertGreater(kwargs["timeout"], 0)

    def test_absent_item_or_locked_keyring_is_an_error_without_a_value(self) -> None:
        with self.assertRaisesRegex(ServiceAccountError, "no 1Password service account token in the keyring"):
            read_token(_runner(returncode=1, stdout=b"partial"))

    def test_empty_item_is_an_error(self) -> None:
        with self.assertRaisesRegex(ServiceAccountError, "empty"):
            read_token(_runner(stdout=b"  \n"))

    def test_missing_secret_tool_is_an_error(self) -> None:
        with self.assertRaisesRegex(ServiceAccountError, "secret-tool is not installed"):
            read_token(_runner(error=FileNotFoundError("secret-tool")))

    def test_a_keyring_that_does_not_answer_is_an_error(self) -> None:
        with self.assertRaisesRegex(ServiceAccountError, "did not answer"):
            read_token(_runner(error=subprocess.TimeoutExpired(KEYRING_LOOKUP, 30)))

    def test_no_error_message_holds_the_token(self) -> None:
        for run in (_runner(returncode=1, stdout=FAKE_TOKEN.encode()), _runner(error=OSError(FAKE_TOKEN))):
            with self.assertRaises(ServiceAccountError) as caught:
                read_token(run)
            self.assertNotIn(FAKE_TOKEN, str(caught.exception))


class OpEnvTest(unittest.TestCase):
    def test_adds_the_token_and_leaves_the_base_unchanged(self) -> None:
        base = {"PATH": "/bin", "HOME": "/home/agent"}

        env = op_env(base, FAKE_TOKEN)

        self.assertEqual(env, {"PATH": "/bin", "HOME": "/home/agent", "OP_SERVICE_ACCOUNT_TOKEN": FAKE_TOKEN})
        self.assertEqual(base, {"PATH": "/bin", "HOME": "/home/agent"})

    def test_drops_a_connect_server_that_would_take_precedence(self) -> None:
        env = op_env({"OP_CONNECT_HOST": "h", "OP_CONNECT_TOKEN": "t", "PATH": "/bin"}, FAKE_TOKEN)

        self.assertEqual(sorted(env), ["OP_SERVICE_ACCOUNT_TOKEN", "PATH"])


class RunOpTest(unittest.TestCase):
    def test_runs_the_command_with_the_token_in_its_environment_only(self) -> None:
        run = _runner(stdout=b"value")

        self.assertEqual(run_op(["op", "x"], {"PATH": "/bin"}, lambda: FAKE_TOKEN, run), b"value")
        (command, kwargs), = run.calls
        self.assertEqual(command, ["op", "x"])
        self.assertEqual(kwargs["env"], {"PATH": "/bin", "OP_SERVICE_ACCOUNT_TOKEN": FAKE_TOKEN})
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertGreater(kwargs["timeout"], 0)

    def test_without_a_token_the_command_never_runs(self) -> None:
        def no_token() -> str:
            raise ServiceAccountError("no token")

        run = _runner()
        with self.assertRaises(ServiceAccountError):
            run_op(["op"], {}, no_token, run)
        self.assertEqual(run.calls, [])

    def test_failing_or_missing_op_is_an_error_without_output(self) -> None:
        for run in (_runner(returncode=1, stdout=b"secret"), _runner(error=FileNotFoundError("op"))):
            with self.assertRaises(ServiceAccountError) as caught:
                run_op(["op"], {}, lambda: FAKE_TOKEN, run)
            self.assertNotIn("secret", str(caught.exception))


class EntryPointTest(unittest.TestCase):
    """python3 -I does not put the script directory on sys.path: the scripts must import the helper anyway."""

    def test_both_scripts_import_the_helper_under_isolated_mode(self) -> None:
        factory = Path(__file__).resolve().parents[1]
        for script in ("as_agent.py", "jira.py"):
            result = subprocess.run(
                [sys.executable, "-I", str(factory / script)], capture_output=True, text=True, check=False
            )
            self.assertEqual(result.returncode, 2, script)
            self.assertNotIn("ModuleNotFoundError", result.stderr, script)


if __name__ == "__main__":
    unittest.main()
