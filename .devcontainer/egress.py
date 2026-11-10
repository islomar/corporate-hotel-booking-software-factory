#!/usr/bin/env python3
"""Allow list and egress check of the devcontainer (CHB-31).

`allowed-hosts.txt` is the single source of the hosts the container may reach.
Commands:
  hosts   print the validated host names, one per line (init-firewall.sh reads them)
  check   connect to one allowed host and one blocked host; exit non-zero unless the
          allowed one connects and the blocked one does not
Exit codes of `check`: 0 ok, 1 the blocked host connected, 2 the allowed host did not
connect, 3 the probe hosts do not match the allow list.
"""

from __future__ import annotations

import re
import socket
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

ALLOW_LIST = Path(__file__).resolve().with_name("allowed-hosts.txt")
ALLOWED_PROBE = "registry.npmjs.org"
BLOCKED_PROBE = "example.com"
PORT = 443
TIMEOUT_SECONDS = 5
HOST = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")

Connect = Callable[[str], bool]


def parse_allowlist(text: str) -> list[str]:
    """Host names of the file. A host is a bare lowercase DNS name: no scheme, port, path, wildcard or IP."""
    hosts: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        host = line.split("#", 1)[0].strip().lower()
        if not host:
            continue
        if not HOST.match(host):
            raise ValueError(f"line {number}: {host!r} is not a bare host name")
        if host in hosts:
            raise ValueError(f"line {number}: {host!r} is listed twice")
        hosts.append(host)
    if not hosts:
        raise ValueError("the allow list is empty")
    return hosts


def tcp_connect(host: str) -> bool:
    try:
        with socket.create_connection((host, PORT), timeout=TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


def check(allowed_host: str, blocked_host: str, allowed: Sequence[str], connect: Connect) -> int:
    if allowed_host not in allowed:
        print(f"FAIL: probe {allowed_host} is not on the allow list")
        return 3
    if blocked_host in allowed:
        print(f"FAIL: probe {blocked_host} is on the allow list, so it proves nothing")
        return 3
    blocked_connects = connect(blocked_host)
    allowed_connects = connect(allowed_host)
    if blocked_connects:
        print(f"FAIL: {blocked_host} connected, but it is not on the allow list")
        return 1
    if not allowed_connects:
        print(f"FAIL: {allowed_host} did not connect, although it is on the allow list")
        return 2
    print(f"ok: {allowed_host} connects, {blocked_host} is blocked")
    return 0


def main(argv: Sequence[str], connect: Connect = tcp_connect) -> int:
    command = argv[0] if argv else ""
    if command not in ("hosts", "check") or len(argv) != 1:
        print("usage: egress.py hosts | check", file=sys.stderr)
        return 64
    try:
        hosts = parse_allowlist(ALLOW_LIST.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(f"{ALLOW_LIST.name}: {error}", file=sys.stderr)
        return 65
    if command == "hosts":
        print("\n".join(hosts))
        return 0
    return check(ALLOWED_PROBE, BLOCKED_PROBE, hosts, connect)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
