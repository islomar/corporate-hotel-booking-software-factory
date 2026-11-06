import http.server
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from run_e2e import (
    DEFAULT_APP_URL,
    E2eError,
    Probe,
    choose,
    deps_are_stale,
    missing_browser_dirs,
    node_problem,
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

    def test_missing_node(self) -> None:
        problem = node_problem(None, "24")
        assert problem is not None
        self.assertIn("Node 24", problem)


class Choose(unittest.TestCase):
    def test_default_url_with_app_running_reuses_it(self) -> None:
        self.assertEqual(choose(DEFAULT_APP_URL, explicit=False, found=Probe.APP), "reuse")

    def test_default_url_with_nothing_listening_starts_container(self) -> None:
        self.assertEqual(choose(DEFAULT_APP_URL, explicit=False, found=Probe.DOWN), "start")

    def test_explicit_url_with_app_running_reuses_it(self) -> None:
        self.assertEqual(choose("https://x.run.app", explicit=True, found=Probe.APP), "reuse")

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


if __name__ == "__main__":
    unittest.main()
