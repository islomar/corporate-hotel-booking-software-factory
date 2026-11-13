"""init-firewall.sh fails closed (CHB-31, review round 2 of PR #103).

The script runs with fake iptables, ip6tables, ipset, dig and curl on PATH. Each fake records its
arguments, so the tests read the exact order of firewall commands without root or a network.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / ".devcontainer" / "init-firewall.sh"

RECORDER = """#!/usr/bin/env bash
echo "$(basename "$0") $*" >> "$FAKE_LOG"
"""
FAKE_IPSET = RECORDER + """if [ "$1" = list ]; then echo "1.2.3.4"; fi
"""
FAKE_CURL = RECORDER + """echo '{"web":["140.82.112.0/20"],"api":["192.30.252.0/22"],"git":["2a0a:a440::/29"]}'
"""


def fake_dig(answer: str) -> str:
    return RECORDER + f'[ -n "{answer}" ] && echo "{answer}"\nexit 0\n'


class FirewallScriptTest(unittest.TestCase):
    def run_script(self, dig_answer: str) -> tuple[int, list[str]]:
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        bin_dir = folder / "bin"
        bin_dir.mkdir()
        fakes = {
            "iptables": RECORDER,
            "ip6tables": RECORDER,
            "ipset": FAKE_IPSET,
            "curl": FAKE_CURL,
            "dig": fake_dig(dig_answer),
        }
        for name, body in fakes.items():
            path = bin_dir / name
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)
        log = folder / "calls.log"
        env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_LOG": str(log), "HOME": str(folder)}
        result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False)  # noqa: S603,S607
        calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        return result.returncode, [c for c in calls if c.split()[0] in ("iptables", "ip6tables", "ipset")]

    def test_it_never_opens_the_firewall(self) -> None:
        for answer in ("1.2.3.4", ""):
            with self.subTest(dig_answer=answer or "none"):
                _, calls = self.run_script(answer)
                self.assertNotIn("iptables -P OUTPUT ACCEPT", calls)
                self.assertNotIn("iptables -P INPUT ACCEPT", calls)

    def test_a_failed_lookup_leaves_the_firewall_closed(self) -> None:
        code, calls = self.run_script("")
        self.assertNotEqual(code, 0)
        policies = [c for c in calls if c.startswith("iptables -P OUTPUT")]
        self.assertEqual(policies[-1], "iptables -P OUTPUT DROP")
        self.assertIn("iptables -A OUTPUT -o lo -j ACCEPT", calls)
        self.assertNotIn("iptables -A OUTPUT -p tcp --dport 443 -m set --match-set allowed-hosts dst -j ACCEPT", calls)

    def test_a_failed_lookup_keeps_the_previous_allow_list(self) -> None:
        _, calls = self.run_script("")
        self.assertFalse([c for c in calls if c.startswith("ipset swap") or c == "ipset destroy allowed-hosts"])

    def test_the_rules_change_only_after_every_lookup(self) -> None:
        code, calls = self.run_script("1.2.3.4")
        self.assertEqual(code, 0)
        first_rule_change = next(i for i, c in enumerate(calls) if c.startswith(("iptables -F", "iptables -A", "iptables -P")))
        last_lookup_add = max(i for i, c in enumerate(calls) if c.startswith("ipset add"))
        self.assertGreater(first_rule_change, last_lookup_add)

    def test_the_new_list_is_swapped_in_and_the_rules_end_closed(self) -> None:
        _, calls = self.run_script("1.2.3.4")
        self.assertIn("ipset swap allowed-hosts-new allowed-hosts", calls)
        self.assertEqual([c for c in calls if c.startswith("iptables -P OUTPUT")][-1], "iptables -P OUTPUT DROP")
        self.assertEqual(calls.count("iptables -A OUTPUT -j REJECT --reject-with icmp-admin-prohibited"), 1)


if __name__ == "__main__":
    unittest.main()
