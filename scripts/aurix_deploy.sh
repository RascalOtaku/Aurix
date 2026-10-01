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
#                   AURIX_DEPLOY_URL (health URL), AURIX_DEPLOY_DRY=1 (print docker commands instead of running them),
#                   AURIX_DEPLOY_TELEGRAM=0 (skip the Telegram-listener readiness check; on by default).
#
# "Healthy" means ALL of: the container runs, it has not restarted itself, /api/health answers 200, AND (since 2026-09-29) the
# Telegram listener is really working - its task is running and Telegram answered one of its polls recently. The app used to treat a
# failed listener start as "non-critical", so a build that broke Telegram still counted as healthy. The listener check is asked from
# INSIDE the container (loopback-only endpoint /api/health/telegram, booleans only - no token is used or printed). If Telegram is not
# configured at all, that check passes and says so.
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

telegram_ready() {                # the listener task runs AND Telegram answered a poll recently (see header); off with AURIX_DEPLOY_TELEGRAM=0
    [ "${AURIX_DEPLOY_TELEGRAM:-1}" = "0" ] && return 0
    docker compose exec -T "$SERVICE" python3 -c "import urllib.request,sys
try:
    sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:7000/api/health/telegram', timeout=5).status == 200 else 1)
except Exception:
    sys.exit(1)" >/dev/null 2>&1
}

healthy() {                       # answers 200 on the health URL, has not restarted itself, and the Telegram listener is ready
    local cid; cid="$(docker compose ps -q "$SERVICE" | head -1)"
    [ -n "$cid" ] || return 1
    [ "$(docker inspect -f '{{.State.Running}}' "$cid" 2>/dev/null)" = "true" ] || return 1
    [ "$(docker inspect -f '{{.RestartCount}}' "$cid" 2>/dev/null)" = "0" ] || return 1
    [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$URL")" = "200" ] || return 1
    telegram_ready
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
        if ! dock compose up -d --build "$SERVICE"; then
            # A restart that was interrupted earlier leaves a "<hash>_<project>-<service>-1" container behind, and
            # every later `compose up` fails on it ("No such container"). Remove those leftovers and try once more.
            stale="$(docker ps -a --format '{{.ID}} {{.Names}}' | awk -v s="$SERVICE" '$2 ~ "^[0-9a-f]+_.*" s "-[0-9]+$" {print $1}')"
            if [ -n "$stale" ] && [ "${AURIX_DEPLOY_DRY:-0}" != "1" ]; then
                say "Removing leftover container(s) from an interrupted restart: $(echo $stale)"
                docker rm -f $stale >/dev/null 2>&1
            fi
            if ! dock compose up -d --build "$SERVICE"; then
                if [ -n "$(docker compose ps -q --status running "$SERVICE" 2>/dev/null)" ]; then
                    say "BUILD FAILED. The previous build is still running (nothing was replaced)."
                else
                    say "BUILD FAILED and $SERVICE is NOT running. Put the last good build back: bash scripts/aurix_deploy.sh rollback"
                fi
                record "build_failed" "-"; exit 1
            fi
        fi
        if [ "${AURIX_DEPLOY_DRY:-0}" = "1" ]; then exit 0; fi
        if wait_healthy; then
            say "DEPLOY OK: the new build is healthy. Go back any time with: bash scripts/aurix_deploy.sh rollback"
            record "ok" "rollback-$stamp"
            # AURIX version history (src/foundation/versions.py): fingerprint the code that shipped; a big shift opens a review ticket.
            # Never fails the deploy: the build is already healthy.
            ver="$(docker compose exec -T "$SERVICE" python3 -c "from src.foundation import versions; r = versions.record('deploy'); print(r['version'], 'BIG SHIFT - review ticket opened' if r.get('big_shift') and r.get('review') == 'open' else '')" 2>/dev/null)" \
                && say "AURIX version: v$ver" || say "AURIX version: could not be recorded (the deploy itself is fine)"
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
