#!/usr/bin/env bash
# Limit outbound traffic of the sandbox container to .devcontainer/allowed-hosts.txt (CHB-31).
# Run as root at every container start, by entry.sh.
# Pattern: Anthropic's Claude Code reference devcontainer,
# https://github.com/anthropics/claude-code/blob/main/.devcontainer/init-firewall.sh
# Differences: the host list is a file validated by egress.py, DNS goes only to the
# resolvers of /etc/resolv.conf, and IPv6 is dropped.
set -euo pipefail
IFS=$'\n\t'

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
set_name="allowed-hosts"

# Fail closed (CHB-31, review of PR #103): on any error, drop all traffic except loopback.
# The rules never open. Lookups run under the rules already in place; on a restart those
# allow DNS to the resolvers and HTTPS to the previous allow list, GitHub's ranges included.
lockdown() {
  iptables -P INPUT DROP
  iptables -P OUTPUT DROP
  iptables -F OUTPUT
  iptables -F INPUT
  iptables -A INPUT -i lo -j ACCEPT
  iptables -A OUTPUT -o lo -j ACCEPT
  echo "init-firewall: failed; all traffic but loopback is blocked. Fix the cause and restart the container." >&2
}
trap 'status=$?; trap - EXIT; if [ "$status" -ne 0 ]; then lockdown; fi; exit "$status"' EXIT

hosts="$(python3 -I "$here/egress.py" hosts)"

# Build the new list in a scratch set. The live set and rules stay as they are until it is complete.
new_set="$set_name-new"
ipset destroy "$new_set" 2>/dev/null || true
ipset create "$new_set" hash:net

# GitHub publishes its address ranges; they change too often to resolve one by one.
meta="$(curl -fsS --max-time 20 https://api.github.com/meta)"
for cidr in $(jq -r '(.web + .api + .git)[]' <<<"$meta" | grep -v ':' | sort -u); do
  ipset add "$new_set" "$cidr" -exist
done

for host in $hosts; do
  ips="$(dig +short +time=3 +tries=2 A "$host" | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' || true)"
  if [ -z "$ips" ]; then
    echo "init-firewall: no address for $host" >&2
    exit 1
  fi
  for ip in $ips; do
    ipset add "$new_set" "$ip" -exist
  done
done

# Every lookup worked: swap the new list in, then rebuild the rules with the policies already closed.
ipset create "$set_name" hash:net -exist
ipset swap "$new_set" "$set_name"
ipset destroy "$new_set"

iptables -P INPUT DROP
iptables -P OUTPUT DROP
iptables -F OUTPUT
iptables -F INPUT

# Loopback, replies, and DNS to the configured resolvers only.
iptables -A INPUT -i lo -j ACCEPT
iptables -A OUTPUT -o lo -j ACCEPT
iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
for resolver in $(awk '/^nameserver/ && $2 ~ /^[0-9.]+$/ {print $2}' /etc/resolv.conf); do
  iptables -A OUTPUT -p udp -d "$resolver" --dport 53 -j ACCEPT
  iptables -A OUTPUT -p tcp -d "$resolver" --dport 53 -j ACCEPT
done

# The allow list, HTTPS only, then answers to established connections, then reject the rest.
iptables -A OUTPUT -p tcp --dport 443 -m set --match-set "$set_name" dst -j ACCEPT
iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -j REJECT --reject-with icmp-admin-prohibited

# No IPv6 route out: the allow list holds IPv4 addresses only.
ip6tables -F
ip6tables -P INPUT DROP
ip6tables -P OUTPUT DROP
ip6tables -P FORWARD DROP
ip6tables -A INPUT -i lo -j ACCEPT
ip6tables -A OUTPUT -o lo -j ACCEPT

echo "init-firewall: $(ipset list "$set_name" | grep -c '^[0-9]') address entries from $(wc -l <<<"$hosts") hosts"
