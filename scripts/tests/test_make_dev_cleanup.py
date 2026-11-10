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
        self.assertIn('PYTHONDONTWRITEBYTECODE: "1"', (ROOT / "compose.yaml").read_text())


class CleanDevTest(unittest.TestCase):
    def test_clean_dev_removes_the_compose_volumes_and_old_loop_state(self) -> None:
        lines = _dry_run("clean-dev")

        self.assertIn("docker compose --profile prod down --volumes --remove-orphans", lines)
        self.assertIn("python3 -I factory/hooks/warn_edit_loops.py --prune", lines)


if __name__ == "__main__":
    unittest.main()
