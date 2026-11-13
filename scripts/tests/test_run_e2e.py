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
    docker_command,
    playwright_image,
    probe,
)


class Image(unittest.TestCase):
    DIGEST = "@sha256:" + "a" * 64

    def test_reads_the_image_of_the_ci_workflow(self) -> None:
        text = f"    env:\n      # note\n      PLAYWRIGHT_IMAGE: mcr.microsoft.com/playwright:v1.65.0-noble{self.DIGEST}\n"
        self.assertEqual(
            playwright_image(text), f"mcr.microsoft.com/playwright:v1.65.0-noble{self.DIGEST}"
        )

    def test_workflow_without_the_image_is_an_error(self) -> None:
        with self.assertRaises(E2eError) as error:
            playwright_image("jobs: {}")
        self.assertIn("PLAYWRIGHT_IMAGE", str(error.exception))

    def test_image_without_a_digest_is_an_error(self) -> None:
        with self.assertRaises(E2eError) as error:
            playwright_image("PLAYWRIGHT_IMAGE: mcr.microsoft.com/playwright:v1.65.0-noble")
        self.assertIn("digest", str(error.exception))

    def test_the_repository_workflow_names_a_pinned_image(self) -> None:
        self.assertRegex(
            run_e2e.ci_image(), r"^mcr\.microsoft\.com/playwright:v[\d.]+-[a-z]+@sha256:[0-9a-f]{64}$"
        )


class DockerCommand(unittest.TestCase):
    def setUp(self) -> None:
        self.command = docker_command("img:1", ["npm", "ci"], uid=1000, gid=1001)

    def test_runs_as_the_host_user_so_no_file_is_root_owned(self) -> None:
        self.assertEqual(self.command[self.command.index("--user") + 1], "1000:1001")

    def test_uses_the_ci_flags(self) -> None:
        for flag in ("--rm", "--network", "--ipc"):
            self.assertIn(flag, self.command)
        self.assertEqual(self.command[self.command.index("--network") + 1], "host")
        self.assertEqual(self.command[self.command.index("--ipc") + 1], "host")

    def test_mounts_e2e_and_ends_with_image_and_command(self) -> None:
        self.assertIn(f"{run_e2e.E2E}:/e2e", self.command)
        self.assertEqual(self.command[-3:], ["img:1", "npm", "ci"])


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

    def test_program_that_does_not_speak_http(self) -> None:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        self.addCleanup(listener.close)

        def answer() -> None:
            connection, _ = listener.accept()
            with connection:
                connection.recv(1024)
                connection.sendall(b"SSH-2.0-OpenSSH\r\n")

        threading.Thread(target=answer, daemon=True).start()
        self.assertEqual(probe(f"http://127.0.0.1:{listener.getsockname()[1]}"), Probe.OTHER)

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


class Orchestration(unittest.TestCase):
    """`make e2e` stops only what it started, and never starts anything for an explicit APP_URL."""

    def setUp(self) -> None:
        self.commands: list[list[str]] = []
        self.playwright_exit = 0
        self.playwright_raises: type[BaseException] | None = None
        self.healthy_after_start = True
        self.app_running = False
        for name, value in {
            "ci_image": lambda: "img:1",
            "install_deps": lambda image: None,
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
        if command[:2] == ["docker", "run"] and "./node_modules/.bin/playwright" in command:
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

    def test_app_url_must_be_http(self) -> None:
        os.environ["APP_URL"] = "file:///etc/passwd"
        with self.assertRaises(E2eError) as error:
            run_e2e.run()
        self.assertIn("http://", str(error.exception))
        self.assertEqual(self.commands, [])

    def test_playwright_runs_in_the_image_from_node_modules_never_through_npx(self) -> None:
        self.assertEqual(run_e2e.run(), 0)
        [command] = [c for c in self.commands if c[:2] == ["docker", "run"]]
        self.assertEqual(command[-2:], ["./node_modules/.bin/playwright", "test"])
        self.assertIn("img:1", command)
        self.assertIn("APP_URL=http://localhost:8080", command)
        self.assertNotIn("npx", command)

    def test_arguments_go_to_playwright(self) -> None:
        self.assertEqual(run_e2e.run(["tests/home.spec.ts", "--retries=1"]), 0)
        [command] = [c for c in self.commands if c[:2] == ["docker", "run"]]
        self.assertEqual(command[-3:], ["test", "tests/home.spec.ts", "--retries=1"])

    def test_docker_is_checked_before_anything_else(self) -> None:
        with (
            mock.patch.object(run_e2e, "check_docker", side_effect=E2eError("no docker")),
            self.assertRaises(E2eError),
        ):
            run_e2e.run()
        self.assertEqual(self.commands, [])


if __name__ == "__main__":
    unittest.main()
