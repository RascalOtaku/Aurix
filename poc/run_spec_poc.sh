#!/usr/bin/env bash
# Runs the four isolated spec proofs of concept (Activation Handoff §10 tasks #4-#7) on the 7070.
#   bash poc/run_spec_poc.sh all     start, test everything, print a verdict (leaves the stack running)
#   bash poc/run_spec_poc.sh up | test | down
#   bash poc/run_spec_poc.sh down    stop and DELETE everything (containers, network, volumes); images stay cached
# Secrets are generated once into ~/.aurix-poc.env (mode 600, outside the synced repo) and are never printed.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SECRETS="${AURIX_POC_ENV:-$HOME/.aurix-poc.env}"
COMPOSE=(docker compose -p aurix-poc -f "$HERE/docker-compose.spec-poc.yml")

load_secrets() {
  if [ ! -f "$SECRETS" ]; then
    ( umask 077
      python3 - "$SECRETS" <<'PY'
import secrets, sys
open(sys.argv[1], "w").write(f"IMMUDB_ADMIN_PASSWORD={secrets.token_urlsafe(24)}\nLITELLM_MASTER_KEY=sk-aurix-poc-{secrets.token_urlsafe(24)}\n")
PY
    )
    chmod 600 "$SECRETS"
  fi
  set -a; . "$SECRETS"; set +a
}

run_client() { "${COMPOSE[@]}" run --rm -T client "$@"; }

up() {
  load_secrets
  "${COMPOSE[@]}" up -d nats opa immudb litellm || { echo "compose up failed"; return 1; }
  sleep 4
  "${COMPOSE[@]}" ps --format 'table {{.Name}}\t{{.Status}}'
}

test_all() {
  load_secrets
  declare -A result
  echo "################ #6 NATS"
  out="$(run_client python /poc/nats_poc.py 2>&1)"; rc1=$?; echo "$out"
  marker="$(printf '%s\n' "$out" | sed -n 's/^MARKER_SEQ=//p' | tail -1)"
  if [ "$rc1" -eq 0 ] && [ -n "$marker" ]; then
    echo "--- restarting the NATS server to prove persistence"
    "${COMPOSE[@]}" restart nats >/dev/null 2>&1; sleep 5
    run_client python /poc/nats_poc.py --phase2 "$marker"; rc1=$?
  fi
  result[NATS]=$rc1
  echo "################ #4 OPA";      run_client python /poc/opa_poc.py;  result[OPA]=$?
  echo "################ #5 immudb";   run_client sh -c "pip install -q --disable-pip-version-check immudb-py >/dev/null 2>&1 && python /poc/immudb_poc.py"; result[immudb]=$?
  echo "################ #7 LiteLLM";  run_client python /poc/litellm_poc.py; result[LiteLLM]=$?
  echo
  echo "================ VERDICT"
  bad=0
  for k in OPA immudb NATS LiteLLM; do
    if [ "${result[$k]}" -eq 0 ]; then echo "  PASS  $k"; else echo "  FAIL  $k"; bad=$((bad+1)); fi
  done
  return "$bad"
}

down() {
  load_secrets
  "${COMPOSE[@]}" --profile test down -v --remove-orphans
  echo "left behind: $(docker ps -a --filter name=aurix-poc -q | wc -l) containers, $(docker volume ls -q --filter name=aurix-poc | wc -l) volumes, $(docker network ls -q --filter name=aurix_poc_net | wc -l) networks"
}

case "${1:-all}" in
  up) up ;;
  test) test_all ;;
  down) down ;;
  all) up && test_all ;;
  *) echo "usage: $0 [all|up|test|down]"; exit 2 ;;
esac
