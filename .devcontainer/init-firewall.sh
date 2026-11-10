#!/usr/bin/env bash
# Limit outbound traffic of the devcontainer to .devcontainer/allowed-hosts.txt (CHB-31).
# Run as root at every container start (devcontainer.json, postStartCommand).
# Pattern: Anthropic's Claude Code reference devcontainer,
# https://github.com/anthropics/claude-code/blob/main/.devcontainer/init-firewall.sh
# Differences: the host list is a file validated by egress.py, DNS goes only to the
# resolvers of /etc/resolv.conf, and IPv6 is dropped.
set -euo pipefail
IFS=$'\n\t'

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
set_name="allowed-hosts"

hosts="$(python3 -I "$here/egress.py" hosts)"

# Reset to an open firewall first, so the lookups below work on a restart.
iptables -P INPUT ACCEPT
iptables -P OUTPUT ACCEPT
iptables -F OUTPUT
iptables -F INPUT
ipset destroy "$set_name" 2>/dev/null || true
ipset create "$set_name" hash:net

# GitHub publishes its address ranges; they change too often to resolve one by one.
meta="$(curl -fsS --max-time 20 https://api.github.com/meta)"
for cidr in $(jq -r '(.web + .api + .git)[]' <<<"$meta" | grep -v ':' | sort -u); do
  ipset add "$set_name" "$cidr" -exist
done

for host in $hosts; do
  ips="$(dig +short +time=3 +tries=2 A "$host" | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' || true)"
  if [ -z "$ips" ]; then
    echo "init-firewall: no address for $host" >&2
    exit 1
  fi
  for ip in $ips; do
    ipset add "$set_name" "$ip" -exist
  done
done

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

iptables -P INPUT DROP
iptables -P OUTPUT DROP

# No IPv6 route out: the allow list holds IPv4 addresses only.
ip6tables -F
ip6tables -P INPUT DROP
ip6tables -P OUTPUT DROP
ip6tables -P FORWARD DROP
ip6tables -A INPUT -i lo -j ACCEPT
ip6tables -A OUTPUT -o lo -j ACCEPT

echo "init-firewall: $(ipset list "$set_name" | grep -c '^[0-9]') address entries from $(wc -l <<<"$hosts") hosts"
