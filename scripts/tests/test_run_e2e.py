import http.server
import os
import socket
import sys
import tempfile
import threading
import time
import subprocess
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import run_e2e
from run_e2e import (
    DEFAULT_APP_URL,
    Action,
    E2eError,
    Probe,
    choose,
    deps_are_stale,
    missing_browser_dirs,
    node_problem,
    npm_problem,
    probe,
)


class NodeVersion(unittest.TestCase):
    def test_required_major_passes(self) -> None:
        self.assertIsNone(node_problem("v24.22.2\n", "24\n"))

    def test_newer_major_passes(self) -> None:
        self.assertIsNone(node_problem("v26.1.0", "24"))

    def test_older_major_names_versions_and_nvm(self) -> None:
        problem = node_problem("v22.14.0", "24")
        assert problem is not None
        self.assertIn("v22.14.0", problem)
        self.assertIn("24", problem)
        self.assertIn("nvm use", problem)

    def test_unparsable_node_version_is_a_problem(self) -> None:
        problem = node_problem("node.js 24", "24")
        assert problem is not None
        self.assertIn("node.js 24", problem)

    def test_nvmrc_alias_is_a_clear_error(self) -> None:
        with self.assertRaises(E2eError) as error:
            node_problem("v24.1.0", "lts/*")
        self.assertIn("lts/*", str(error.exception))

    def test_missing_node(self) -> None:
        problem = node_problem(None, "24")
        assert problem is not None
        self.assertIn("Node 24", problem)


class NpmVersion(unittest.TestCase):
    def test_new_enough(self) -> None:
        self.assertIsNone(npm_problem("11.10.0\n"))
        self.assertIsNone(npm_problem("12.0.1"))

    def test_too_old_to_honour_min_release_age(self) -> None:
        problem = npm_problem("11.9.3")
        assert problem is not None
        self.assertIn("11.9.3", problem)
        self.assertIn("min-release-age", problem)

    def test_missing(self) -> None:
        self.assertIsNotNone(npm_problem(None))


class Choose(unittest.TestCase):
    def test_default_url_with_app_running_reuses_it(self) -> None:
        self.assertEqual(choose(DEFAULT_APP_URL, explicit=False, found=Probe.APP), Action.REUSE)

    def test_default_url_with_nothing_listening_starts_container(self) -> None:
        self.assertEqual(choose(DEFAULT_APP_URL, explicit=False, found=Probe.DOWN), Action.START)

    def test_explicit_url_with_app_running_reuses_it(self) -> None:
        self.assertEqual(choose("https://x.run.app", explicit=True, found=Probe.APP), Action.REUSE)

    def test_explicit_url_never_starts_a_container(self) -> None:
        with self.assertRaises(E2eError) as error:
            choose("http://localhost:9999", explicit=True, found=Probe.DOWN)
        self.assertIn("http://localhost:9999", str(error.exception))
        self.assertIn("unset APP_URL", str(error.exception))

    def test_port_used_by_another_program(self) -> None:
        with self.assertRaises(E2eError) as error:
            choose(DEFAULT_APP_URL, explicit=False, found=Probe.OTHER)
        self.assertIn("8080", str(error.exception))
        self.assertIn("something else", str(error.exception))


class _Handler(http.server.BaseHTTPRequestHandler):
    body = b""
    status = 200

    def do_GET(self) -> None:
        self.send_response(self.status)
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args: object) -> None:
        pass


def _serve(status: int, body: bytes) -> tuple[http.server.HTTPServer, str]:
    handler = type("H", (_Handler,), {"status": status, "body": body})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def _stop(server: http.server.HTTPServer) -> None:
    server.shutdown()
    server.server_close()


class ProbeUrl(unittest.TestCase):
    def test_healthy_app(self) -> None:
        server, url = _serve(200, b'{"status":"ok","version":"local"}')
        self.addCleanup(_stop, server)
        self.assertEqual(probe(url), Probe.APP)

    def test_other_program(self) -> None:
        server, url = _serve(404, b"not here")
        self.addCleanup(_stop, server)
        self.assertEqual(probe(url), Probe.OTHER)

    def test_other_program_with_non_json_body(self) -> None:
        server, url = _serve(200, b"<html></html>")
        self.addCleanup(_stop, server)
        self.assertEqual(probe(url), Probe.OTHER)

    def test_nothing_listening(self) -> None:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.assertEqual(probe(f"http://127.0.0.1:{port}"), Probe.DOWN)


class Deps(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        (self.dir / "package-lock.json").write_text("{}")

    def _install(self, age: float) -> None:
        installed = self.dir / "node_modules/.package-lock.json"
        installed.parent.mkdir(exist_ok=True)
        installed.write_text("{}")
        stamp = time.time() + age
        os.utime(installed, (stamp, stamp))

    def test_fresh_clone(self) -> None:
        self.assertTrue(deps_are_stale(self.dir))

    def test_installed_after_lockfile(self) -> None:
        self._install(age=10)
        self.assertFalse(deps_are_stale(self.dir))

    def test_lockfile_changed_after_install(self) -> None:
        self._install(age=-10)
        self.assertTrue(deps_are_stale(self.dir))


class Browsers(unittest.TestCase):
    def test_lists_install_locations_that_do_not_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            present = Path(tmp, "chromium-1")
            present.mkdir()
            dry_run = (
                f"Chrome (playwright chromium v1)\n  Install location:    {present}\n"
                f"  Download url: x\n\nFFmpeg\n  Install location:    {tmp}/ffmpeg-1\n"
            )
            self.assertEqual(missing_browser_dirs(dry_run), [f"{tmp}/ffmpeg-1"])


class Orchestration(unittest.TestCase):
    """`make e2e` stops only what it started, and never starts anything for an explicit APP_URL."""

    def setUp(self) -> None:
        self.commands: list[list[str]] = []
        self.playwright_exit = 0
        self.playwright_raises: type[BaseException] | None = None
        self.healthy_after_start = True
        self.app_running = False
        for name, value in {
            "tool_version": lambda tool: "v24.1.0",
            "install_deps": lambda: None,
            "check_docker": lambda: None,
            "probe": lambda url: Probe.APP if self.app_running else Probe.DOWN,
            "say": lambda message: None,
        }.items():
            patcher = mock.patch.object(run_e2e, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(run_e2e.signal, "signal")
        self.signal = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(run_e2e.subprocess, "run", self._run)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("APP_URL", None)

    def _run(self, command: list[object], **kwargs: object) -> subprocess.CompletedProcess[str]:
        command = [str(part) for part in command]
        self.commands.append(command)
        if command[:2] == ["docker", "compose"] and "up" in command:
            self.app_running = self.healthy_after_start
        if command[0].endswith("playwright") and command[1] == "test":
            if self.playwright_raises:
                raise self.playwright_raises
            return subprocess.CompletedProcess(command, self.playwright_exit)
        return subprocess.CompletedProcess(command, 0)

    def compose(self, action: str) -> list[list[str]]:
        return [c for c in self.commands if c[:2] == ["docker", "compose"] and action in c]

    def test_starts_and_removes_the_container_when_nothing_answers(self) -> None:
        self.assertEqual(run_e2e.run(), 0)
        self.assertEqual(len(self.compose("up")), 1)
        self.assertEqual(self.compose("rm")[0][-3:], ["--stop", "--force", "app"])

    def test_failing_tests_still_remove_the_container_and_keep_the_exit_code(self) -> None:
        self.playwright_exit = 1
        self.assertEqual(run_e2e.run(), 1)
        self.assertEqual(len(self.compose("rm")), 1)

    def test_interrupt_removes_the_container(self) -> None:
        self.playwright_raises = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            run_e2e.run()
        self.assertEqual(len(self.compose("rm")), 1)
        self.signal.assert_any_call(run_e2e.signal.SIGINT, run_e2e.signal.SIG_IGN)

    def test_container_that_never_gets_healthy_is_removed(self) -> None:
        self.healthy_after_start = False
        with (
            mock.patch.object(run_e2e, "HEALTH_TIMEOUT_SECONDS", 0),
            mock.patch.object(run_e2e.time, "sleep"),
            self.assertRaises(E2eError),
        ):
            run_e2e.run()
        self.assertEqual(len(self.compose("rm")), 1)

    def test_reuses_a_running_app_and_leaves_it_running(self) -> None:
        self.app_running = True
        self.assertEqual(run_e2e.run(), 0)
        self.assertEqual(self.compose("compose"), [])

    def test_explicit_app_url_never_touches_docker(self) -> None:
        os.environ["APP_URL"] = "http://localhost:9999"
        with self.assertRaises(E2eError):
            run_e2e.run()
        self.assertEqual(self.commands, [])

    def test_old_node_stops_before_anything_else(self) -> None:
        with (
            mock.patch.object(run_e2e, "tool_version", lambda tool: "v22.14.0"),
            self.assertRaises(E2eError),
        ):
            run_e2e.run()
        self.assertEqual(self.commands, [])


if __name__ == "__main__":
    unittest.main()
