#!/usr/bin/env bash
# Proves the sandbox's TOOLS work and its model path is locked down. Run from odysseus/ on the
# Docker host AFTER `docker compose up -d --build` (and after verify_isolation.sh says VERIFIED).
# Every result is PASS / FAIL / WARN; the last line says what to do next. Nothing here needs the
# internet, a GPU, or real data: ct_to_stl builds a synthetic skull and meshes it; transcribe checks
# its formatting logic plus that ffmpeg and faster-whisper are installed (run one real audio file
# afterwards to prove the model itself); the gateway checks talk to the proxy, not to any model.
set -u
DC="docker compose exec -T sandbox"
fails=0; warns=0
pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1"; fails=$((fails + 1)); }
warn() { echo "WARN  $1"; warns=$((warns + 1)); }

run_selftest() {   # name, command...   (must succeed)
  local name="$1"; shift
  if out=$($DC "$@" 2>&1); then pass "$name  ->  $(echo "$out" | tail -1 | cut -c1-90)"
  else fail "$name  ->  $(echo "$out" | tail -3 | tr '\n' ' ' | cut -c1-220)"; fi
}

echo "== tool selftests (inside the sandbox image) =="
run_selftest "print_cost"  python /opt/tools/print_cost.py --selftest
run_selftest "transcribe"  python /opt/tools/transcribe.py --selftest
run_selftest "ct_to_stl"   python /opt/tools/ct_to_stl.py --selftest

echo "== OpenHands (optional layer) =="
if out=$($DC python /opt/tools/openhands_run.py --selftest 2>&1); then
  if echo "$out" | grep -q "NOT available"; then
    warn "OpenHands SDK not installed in this image (software_dev missions fall back to the chat agent)"
  else
    pass "OpenHands SDK loads  ->  $(echo "$out" | tail -1 | cut -c1-90)"
  fi
else
  warn "OpenHands selftest failed: $(echo "$out" | tail -2 | tr '\n' ' ' | cut -c1-200)"
fi

echo "== model path (llm-gateway allowlist) =="
probe() {   # method path comma-separated-acceptable-status-codes
  $DC python - "$1" "$2" "$3" <<'PY'
import sys, urllib.request, urllib.error
method, path, want = sys.argv[1], sys.argv[2], sys.argv[3]
req = urllib.request.Request("http://llm-gateway:11434" + path, method=method,
                             data=b"{}" if method in ("POST", "PUT", "PATCH") else None,
                             headers={"Content-Type": "application/json"})
try:
    code = urllib.request.urlopen(req, timeout=8).status
except urllib.error.HTTPError as e:
    code = e.code
except Exception as e:
    print("unreachable:", type(e).__name__); sys.exit(2)
print(code)
sys.exit(0 if str(code) in want.split(",") else 1)
PY
}
# Blocked paths must be refused (403) by the PROXY itself: that proves the allowlist even when the GPU box is off.
for spec in "POST /api/pull" "POST /api/delete" "POST /api/create" "DELETE /api/delete" "GET /admin" "GET /v1/../api/pull"; do
  set -- $spec
  if r=$(probe "$1" "$2" 403); then pass "blocked: $1 $2 (403)"
  else fail "NOT blocked: $1 $2 -> $r"; fi
done
if r=$(probe GET /v1/models 200); then pass "allowed: GET /v1/models (model server reachable)"
else warn "GET /v1/models -> $r  (gateway is up; the model server itself is unreachable - fine while the GPU box is off)"; fi

echo
if [ "$fails" -eq 0 ]; then
  echo "TOOLS VERIFIED ($warns warning(s))"
else
  echo "TOOLS NOT VERIFIED - $fails failure(s). Missions that need the failed tool will not work; nothing is unsafe."
fi
