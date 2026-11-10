"""A per-user state folder that other users cannot plant files in, for the hooks that keep state (CHB-54, CHB-30)."""

from __future__ import annotations

import os
import stat


def private_folder(folder: str) -> None:
    """Create the folder; refuse one that is a symlink, belongs to another user or others can open."""
    os.makedirs(folder, mode=0o700, exist_ok=True)
    info = os.lstat(folder)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise PermissionError(f"{folder} is not a private folder")
    if info.st_mode & 0o077:
        raise PermissionError(f"{folder} is open to other users")
