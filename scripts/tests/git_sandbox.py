"""Keep a test's git commands inside its own temporary repository.

Git hooks of a linked worktree run with GIT_DIR and GIT_INDEX_FILE set.
`git -C <dir>` does not override them, so a test run from such a hook would write to the outer repository (CHB-124).
"""

import os
import subprocess
from unittest import mock


def _repository_variables():
    # The variables git itself treats as local to one repository: GIT_DIR, GIT_INDEX_FILE, GIT_WORK_TREE, ...
    listed = subprocess.run(["git", "rev-parse", "--local-env-vars"], check=True, capture_output=True, text=True)  # noqa: S603,S607
    return listed.stdout.split()


def isolate_from_outer_repo(case):
    """Clear repository variables and ignore global and system config until the test's cleanup."""
    patcher = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
    patcher.start()
    case.addCleanup(patcher.stop)
    for name in _repository_variables():
        os.environ.pop(name, None)
