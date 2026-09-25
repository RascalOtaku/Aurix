#!/usr/bin/env bash
# aurix_deploy.sh - deploy the app with a safety net: every deploy keeps the previous build, and a build that does not come up is rolled back by itself.
#
#   bash scripts/aurix_deploy.sh              build + start the new code; if it is not healthy within ~3 min, put the previous build back automatically
#   bash scripts/aurix_deploy.sh list         show the builds you can go back to
#   bash scripts/aurix_deploy.sh rollback     go back to the newest saved build (or:  rollback <tag>  for a specific one, see `list`)
#
# What a rollback restores: the app's CODE (the Docker image). What it never touches: data/ (the audit chain, missions, skills, settings) - the
# hash-chained audit log must only ever move forward, and your data is not part of the code. The source files on disk (synced from the PC) stay
# as they are, so the NEXT deploy rebuilds the newer code again; fix or revert the source first if the new code was the problem.
#
# Test knobs (env): AURIX_DEPLOY_KEEP (saved builds to keep, default 5), AURIX_DEPLOY_WAIT (seconds to wait for health, default 180),
#                   AURIX_DEPLOY_URL (health URL), AURIX_DEPLOY_DRY=1 (print docker commands instead of running them).
set -uo pipefail

cd "$(dirname "$0")/.."
SERVICE="${AURIX_DEPLOY_SERVICE:-odysseus}"
IMAGE="${AURIX_DEPLOY_IMAGE:-odysseus-odysseus}"
KEEP="${AURIX_DEPLOY_KEEP:-5}"
WAIT="${AURIX_DEPLOY_WAIT:-180}"
URL="${AURIX_DEPLOY_URL:-http://127.0.0.1:${APP_PORT:-7000}/api/health}"
STATUS="data/deploy_status.json"
LOG="logs/deploy.log"
mkdir -p logs data

dock() { if [ "${AURIX_DEPLOY_DRY:-0}" = "1" ]; then echo "+ docker $*"; else docker "$@"; fi; }
say() { echo "$*"; echo "$(date -Is) $*" >> "$LOG"; }
record() { printf '{"ts":"%s","result":"%s","detail":"%s"}\n' "$(date -Is)" "$1" "$2" > "$STATUS"; }
running_image() { docker inspect -f '{{.Image}}' "$(docker compose ps -q "$SERVICE" 2>/dev/null | head -1)" 2>/dev/null; }
saved_tags() { docker image ls "$IMAGE" --format '{{.Tag}}' | grep '^rollback-' | sort -r; }

healthy() {                       # answers 200 on the health URL, and has not restarted itself
    local cid; cid="$(docker compose ps -q "$SERVICE" | head -1)"
    [ -n "$cid" ] || return 1
    [ "$(docker inspect -f '{{.State.Running}}' "$cid" 2>/dev/null)" = "true" ] || return 1
    [ "$(docker inspect -f '{{.RestartCount}}' "$cid" 2>/dev/null)" = "0" ] || return 1
    [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$URL")" = "200" ]
}

wait_healthy() {
    local waited=0
    while [ "$waited" -lt "$WAIT" ]; do
        healthy && return 0
        sleep 5; waited=$((waited + 5))
    done
    return 1
}

prune() { saved_tags | tail -n +$((KEEP + 1)) | while read -r t; do dock image rm "$IMAGE:$t" >/dev/null 2>&1; done; }

do_rollback() {
    local tag="${1:-$(saved_tags | head -1)}"
    [ -n "$tag" ] || { say "ROLLBACK: there is no saved build to go back to."; return 1; }
    tag="${tag#rollback-}"; tag="rollback-$tag"
    docker image inspect "$IMAGE:$tag" >/dev/null 2>&1 || { say "ROLLBACK: no saved build called $tag (try: list)"; return 1; }
    say "ROLLBACK: putting build $tag back..."
    dock tag "$IMAGE:$tag" "$IMAGE:latest" && dock compose up -d --no-build "$SERVICE"
    if [ "${AURIX_DEPLOY_DRY:-0}" = "1" ]; then return 0; fi
    if wait_healthy; then
        say "ROLLBACK OK: the previous build ($tag) is running again. data/ was not touched."
        record "rolled_back" "$tag"
        return 0
    fi
    say "ROLLBACK FAILED: $tag did not become healthy either. Look at: docker compose logs --tail 80 $SERVICE"
    record "rollback_failed" "$tag"
    return 2
}

case "${1:-deploy}" in
    list)
        echo "Builds you can go back to (newest first):"
        saved_tags | sed 's/^/  /'
        [ -f "$STATUS" ] && { echo; echo "Last deploy: $(cat "$STATUS")"; }
        ;;
    rollback)
        do_rollback "${2:-}"
        ;;
    deploy)
        prev="$(running_image)"
        stamp="$(date +%Y%m%d-%H%M%S)"
        if [ -n "$prev" ]; then
            dock tag "$prev" "$IMAGE:rollback-$stamp" && say "Saved the running build as rollback-$stamp"
        else
            say "No running build to save (first deploy?)"
        fi
        say "Building and starting the new code..."
        dock compose up -d --build "$SERVICE" || { say "BUILD FAILED. The old build is still running (nothing was replaced)."; record "build_failed" "-"; exit 1; }
        if [ "${AURIX_DEPLOY_DRY:-0}" = "1" ]; then exit 0; fi
        if wait_healthy; then
            say "DEPLOY OK: the new build is healthy. Go back any time with: bash scripts/aurix_deploy.sh rollback"
            record "ok" "rollback-$stamp"
            prune
            exit 0
        fi
        say "DEPLOY UNHEALTHY: the new build did not come up in ${WAIT}s. Rolling back by itself."
        record "unhealthy" "rollback-$stamp"
        do_rollback "$stamp"
        exit 3
        ;;
    *)
        echo "usage: $0 [deploy|list|rollback [tag]]"; exit 64
        ;;
esac
