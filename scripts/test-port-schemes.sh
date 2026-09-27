#!/usr/bin/env bash
# Try each port-publishing scheme against the *current* daemon config and
# report which ones actually serve traffic. Recreates containers only -- no
# daemon restart -- so it is safe to run repeatedly.
#
#   scripts/test-port-schemes.sh
#
# Restores docker-compose.yml on exit.
set -uo pipefail
cd "$(dirname "$0")/.."
COMPOSE=docker-compose.yml
cp "$COMPOSE" "$COMPOSE.orig"
trap 'cp "$COMPOSE.orig" "$COMPOSE"; rm -f "$COMPOSE.orig"' EXIT

set_scheme() {
  python3 - "$1" <<'PY'
import re, sys
from pathlib import Path
scheme = sys.argv[1]
p = Path("docker-compose.yml")
out, idx = [], 0
for ln in p.read_text().splitlines(True):
    if "ports: [" in ln:
        idx += 1
        host = {
            "loopback-single": f"127.0.0.1:{8000 + idx}:8080",
            "loopback-multi":  f"127.0.1.{idx}:8080:8080",
            "wildcard":        f"{8000 + idx}:8080",
        }[scheme]
        ln = re.sub(r'ports: \[".*"\]', f'ports: ["{host}"]', ln)
    out.append(ln)
p.write_text("".join(out))
print(f"  rewrote {idx} port mappings")
PY
}

url_for() {  # $1=scheme $2=index
  case "$1" in
    loopback-multi) echo "http://127.0.1.$2:8080/api/hello" ;;
    *)              echo "http://127.0.0.1:$((8000 + $2))/api/hello" ;;
  esac
}

count_ok() {
  local ok=0
  for i in $(seq 1 10); do
    curl -fsS --max-time 3 "$(url_for "$1" "$i")" >/dev/null 2>&1 && ok=$((ok + 1))
  done
  echo "$ok"
}

cfg=/var/snap/docker/current/config/daemon.json
echo "userland-proxy: $(grep -o '"userland-proxy":[^,}]*' "$cfg" 2>/dev/null || echo '(unset -> default true)')"
echo

for scheme in loopback-single loopback-multi wildcard; do
  echo "=== $scheme"
  set_scheme "$scheme"
  docker compose down >/dev/null 2>&1
  if ! docker compose up -d >/tmp/ps-up.log 2>&1; then
    echo "  FAILED to start:"; tail -4 /tmp/ps-up.log | sed 's/^/    /'
    continue
  fi
  for _ in $(seq 1 20); do [ "$(count_ok "$scheme")" -eq 10 ] && break; sleep 2; done
  echo "  endpoints responding: $(count_ok "$scheme")/10   docker-proxy procs: $(pgrep -c docker-proxy 2>/dev/null || echo 0)"
done

docker compose down >/dev/null 2>&1
echo
echo "done"
