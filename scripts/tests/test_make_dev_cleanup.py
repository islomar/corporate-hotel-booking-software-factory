"""CHB-138: `make dev` leaves no root-owned files in the checkout; `make clean-dev` removes its leftovers."""

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _dry_run(target: str) -> list[str]:
    result = subprocess.run(["make", "-n", "-C", str(ROOT), target], capture_output=True, text=True, check=True)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


class MakeDevTest(unittest.TestCase):
    def test_dev_creates_node_modules_as_the_host_user_before_compose_starts(self) -> None:
        lines = _dry_run("dev")
        mkdir = next(i for i, line in enumerate(lines) if line == "mkdir -p frontend/node_modules")
        up = next(i for i, line in enumerate(lines) if line.startswith("docker compose up"))

        self.assertLess(mkdir, up)

    def test_the_backend_container_writes_no_bytecode_into_the_bind_mount(self) -> None:
        self.assertIn('PYTHONDONTWRITEBYTECODE: "1"', _service_environment("backend"))


def _service_environment(service: str) -> list[str]:
    """The uncommented `KEY: value` lines under services.<service>.environment in compose.yaml.

    A small reader for this file's layout (2-space indent), since `python3 -I` has no YAML parser."""
    lines, in_service, in_environment = [], False, False
    for line in (ROOT / "compose.yaml").read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent <= 2:
            in_service, in_environment = indent == 2 and stripped == f"{service}:", False
        elif in_service and indent == 4:
            in_environment = stripped == "environment:"
        elif in_environment and indent == 6:
            lines.append(stripped)
    return lines


class CleanDevTest(unittest.TestCase):
    def test_clean_dev_removes_the_compose_volumes_and_old_loop_state(self) -> None:
        lines = _dry_run("clean-dev")

        self.assertIn("docker compose --profile prod down --volumes --remove-orphans", lines)
        self.assertIn("python3 -I factory/hooks/warn_edit_loops.py --prune", lines)


if __name__ == "__main__":
    unittest.main()
