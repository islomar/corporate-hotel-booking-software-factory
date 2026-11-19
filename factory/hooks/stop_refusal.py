#!/usr/bin/env python3
"""Refuse a Stop once per session for the same findings: shared by the Stop hooks (CHB-162, CHB-209).

A Stop hook that exits 2 sends the agent back. When the agent cannot act on the findings, a second
refusal for the same findings would loop. `already_refused` records the fingerprint of the findings of
each refusal, per session, in `<folder>/<session id>.json`, and answers True on a repeat.

Standard library only, so a hook that imports it does not depend on Jira or another hook.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # python3 -I does not add it

from private_folder import private_folder  # noqa: E402


def _state_path(folder: str, session_id: str) -> str:
    return posixpath.join(folder, re.sub(r"[^A-Za-z0-9_-]", "_", session_id or "none") + ".json")


def already_refused(folder: str, session_id: str, fingerprint: str | None) -> bool:
    """Record the findings of this stop. True when this session was already refused for the same findings.

    None (no findings) forgets the refusal. Raises OSError when the folder cannot be used.
    """
    path = _state_path(folder, session_id)
    if fingerprint is None:
        if os.path.lexists(folder):
            private_folder(folder)
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        return False
    private_folder(folder)
    try:
        with open(path, encoding="utf-8") as state:
            if json.load(state).get("refused") == fingerprint:
                return True
    except (FileNotFoundError, ValueError, AttributeError):
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as state:
        json.dump({"refused": fingerprint}, state)
    return False
