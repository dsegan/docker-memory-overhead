#!/usr/bin/env bash
# Thin wrapper around memreport.py.
#
#   scripts/report.sh baseline          # with the stack DOWN
#   scripts/report.sh report            # with the stack UP
#   scripts/report.sh report --requests 200
#
# Prefers running as root, because PSS (/proc/<pid>/smaps_rollup) is only
# readable by root and PSS is what gives an honest total for the 20-odd
# shim/proxy processes that share the same text pages. Falls back to an
# unprivileged run -- the report says so and marks its figures as RSS.
set -euo pipefail

cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}
SCRIPT=scripts/memreport.py

if [[ $EUID -eq 0 || ${MEMREPORT_NO_SUDO:-0} == 1 ]]; then
  exec "$PY" "$SCRIPT" "$@"
fi

if sudo -n true 2>/dev/null; then
  exec sudo -n -E "$PY" "$SCRIPT" "$@"
fi

echo "note: no passwordless sudo -- measuring host processes by RSS instead of PSS." >&2
echo "      for exact shared-page accounting: sudo $PY $SCRIPT $*" >&2
exec "$PY" "$SCRIPT" "$@"
