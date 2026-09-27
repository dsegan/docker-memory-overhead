#!/usr/bin/env bash
# Thin wrapper around memreport.py.
#
#   scripts/report.sh baseline          # with the stack DOWN
#   scripts/report.sh report            # with the stack UP
#   scripts/report.sh report --requests 200
#
# Prefers running as root, because daemon PSS (/proc/<pid>/smaps_rollup)
# usually requires elevated permissions. PSS apportions shared pages for the
# shim/proxy processes that share the same text pages. Falls back to an
# unprivileged run -- the collector checks actual PSS availability per process.
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

echo "note: no passwordless sudo -- unreadable PSS will fall back to RSS." >&2
echo "      for daemon PSS access: sudo $PY $SCRIPT $*" >&2
exec "$PY" "$SCRIPT" "$@"
