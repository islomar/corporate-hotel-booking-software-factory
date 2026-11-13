#!/usr/bin/env bash
# Entry point of the sandbox container (CHB-31). Runs as root, then drops to `agent`.
# 1. Set the firewall (init-firewall.sh). If it fails, it blocks all traffic but loopback and
#    this script stops: the command never runs.
# 2. Give `agent` the node_modules volume, and run `npm ci` in frontend/ when the lock file
#    changed since the last install in this volume.
# 3. Run the command as `agent`: no capabilities, no new privileges.
set -euo pipefail

here=/usr/local/share/devcontainer
"$here/init-firewall.sh" >/dev/null

as_agent() {
  exec_word=""
  if [ "$1" = "--exec" ]; then exec_word=exec; shift; fi
  $exec_word setpriv --reuid=agent --regid=agent --init-groups --inh-caps=-all --bounding-set=-all --no-new-privs \
    env HOME=/home/agent USER=agent LOGNAME=agent "$@"
}

if [ -f frontend/package-lock.json ] && [ -d frontend/node_modules ]; then
  chown agent:agent frontend/node_modules
  stamp=frontend/node_modules/.chb-sandbox-lock
  if ! cmp -s frontend/package-lock.json "$stamp"; then
    echo "entry: npm ci in frontend/ (lock file changed)" >&2
    as_agent bash -c 'cd frontend && npm ci --no-audit --no-fund >/dev/null && cp package-lock.json node_modules/.chb-sandbox-lock'
  fi
fi

as_agent --exec "$@"
