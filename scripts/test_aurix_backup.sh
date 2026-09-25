#!/usr/bin/env bash
# Self-test for aurix_backup.sh. Runs entirely in a throwaway temp directory (never touches the real NAS or real AURIX data).
#   bash scripts/test_aurix_backup.sh          (Linux; needs rsync, python3)
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/aurix_backup.sh"
T="$(mktemp -d /tmp/aurix_backup_test.XXXXXX)"
HOLD_PID=""
cleanup() { [ -n "$HOLD_PID" ] && kill "$HOLD_PID" 2>/dev/null; chmod -R u+rwX "$T" 2>/dev/null; rm -rf "$T"; }
trap cleanup EXIT
pass=0; failn=0
ok()  { pass=$((pass+1)); echo "  PASS  $1"; }
bad() { failn=$((failn+1)); echo "  FAIL  $1"; }
check() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }

NAS="$T/nas"; BRAIN="$T/brain"; APP="$T/app"; STATUS="$T/status.json"; LOG="$T/log"; SNAPS="$NAS/aurix-backup/snapshots"
mkdir -p "$NAS/aurix-backup" "$BRAIN/memory/hot" "$BRAIN/wiki" "$BRAIN/.chroma" "$BRAIN/runtime" "$BRAIN/venv" \
         "$APP/sub" "$APP/ssh" "$APP/fastembed_cache" "$APP/workspace/m-1"
echo mem > "$BRAIN/memory/hot/a.md"; echo wiki > "$BRAIN/wiki/w.md"; echo chroma > "$BRAIN/.chroma/c"; echo rt > "$BRAIN/runtime/r.json"; echo junk > "$BRAIN/venv/x"
echo SECRET=1 > "$APP/.env"; echo '{"k":1}' > "$APP/auth.json"; echo key > "$APP/ssh/id"; echo pem > "$APP/x.pem"; echo k > "$APP/x.key"; echo c > "$APP/credentials.txt"
echo keep > "$APP/keep.txt"; echo '{}' > "$APP/sub/keep2.json"; echo model > "$APP/fastembed_cache/m"; echo big > "$APP/workspace/m-1/out"

# a LIVE SQLite database in WAL mode: 100 committed rows that still sit in the -wal file, with a writer holding it open
python3 - "$APP/app.db" <<'PY' &
import sqlite3, sys, time
c = sqlite3.connect(sys.argv[1]); c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA wal_autocheckpoint=0")
c.execute("CREATE TABLE t (i INTEGER)"); c.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(100)]); c.commit()
open(sys.argv[1] + ".ready", "w").write("1"); time.sleep(120)
PY
HOLD_PID=$!
for _ in $(seq 1 50); do [ -e "$APP/app.db.ready" ] && break; sleep 0.2; done
rm -f "$APP/app.db.ready"

run() {  # run <stamp> [extra env...]  -> exit code of the script
  local stamp="$1"; shift
  env AURIX_BACKUP_NAS="$NAS" AURIX_BRAIN_DIR="$BRAIN" AURIX_APP_DATA="$APP" AURIX_BACKUP_STATUS="$STATUS" AURIX_BACKUP_LOG="$LOG" \
      AURIX_BACKUP_REQUIRE_MOUNT=0 AURIX_BACKUP_STAMP="$stamp" TMPDIR="$T" "$@" bash "$SCRIPT" >/dev/null 2>&1
}
jq_py() { python3 -c "import json,sys; d=json.load(open('$STATUS')); print($1)"; }

echo "== guards"
env AURIX_BACKUP_NAS="$NAS" AURIX_BRAIN_DIR="$BRAIN" AURIX_APP_DATA="$APP" AURIX_BACKUP_STATUS="$STATUS" AURIX_BACKUP_LOG="$LOG" \
    AURIX_BACKUP_STAMP=2026-09-19_0300 TMPDIR="$T" bash "$SCRIPT" >/dev/null 2>&1; rc=$?
check "refuses when the NAS is not a real mount (default settings)" "[ $rc -ne 0 ]"
check "  ...and says why in the status file" "[ \"\$(jq_py \"'not mounted' in d['message']\")\" = True ]"
check "  ...and wrote NOTHING into the local mount point" "[ ! -e '$SNAPS' ]"
run 2026-09-19_0300 AURIX_BACKUP_NAS="$T/empty-nas"; rc=$?
check "refuses when the marker folder is missing on the NAS" "[ $rc -ne 0 ] && [ \"\$(jq_py \"'marker folder' in d['message']\")\" = True ]"
run 2026-09-19_0300 AURIX_BRAIN_DIR="$T/nope"; rc=$?
check "refuses when the brain directory is missing" "[ $rc -ne 0 ]"
run "not-a-stamp"; rc=$?
check "rejects a malformed stamp" "[ $rc -eq 2 ]"

echo "== first backup"
run 2026-09-19_0300; rc=$?
S1="$SNAPS/2026-09-19_0300"
check "succeeds" "[ $rc -eq 0 ] && [ -d '$S1' ]"
check "no .partial left behind" "[ -z \"\$(ls '$SNAPS' | grep partial)\" ]"
check "'latest' points at it" "[ \"\$(readlink '$SNAPS/latest')\" = 2026-09-19_0300 ]"
check "brain: memory, wiki, .chroma, runtime copied" "[ -f '$S1/brain/memory/hot/a.md' ] && [ -f '$S1/brain/wiki/w.md' ] && [ -f '$S1/brain/.chroma/c' ] && [ -f '$S1/brain/runtime/r.json' ]"
check "brain: the venv is NOT copied" "[ ! -e '$S1/brain/venv' ]"
check "app: ordinary files copied (incl. subfolders)" "[ -f '$S1/app/keep.txt' ] && [ -f '$S1/app/sub/keep2.json' ]"
for secret in .env auth.json ssh x.pem x.key credentials.txt; do
  check "SECRET not copied: $secret" "[ ! -e '$S1/app/$secret' ]"
done
check "big re-creatable data skipped (model cache, mission workspaces)" "[ ! -e '$S1/app/fastembed_cache' ] && [ ! -e '$S1/app/workspace' ]"
check "no stray sqlite side files" "[ -z \"\$(ls '$S1/app' | grep -E 'db-(wal|shm)')\" ]"
check "LIVE WAL database copied CONSISTENTLY (all 100 rows present)" \
      "[ \"\$(python3 -c \"import sqlite3; c=sqlite3.connect('$S1/app/app.db'); print(c.execute('select count(*) from t').fetchone()[0], c.execute('pragma integrity_check').fetchone()[0])\")\" = '100 ok' ]"
check "status file says ok and records last_ok" "[ \"\$(jq_py \"d['ok'] and d['last_ok'] is not None and d['snapshot']\")\" = 2026-09-19_0300 ]"

echo "== second backup: versioned + hard-linked"
echo "changed" > "$APP/keep.txt"
run 2026-09-20_0300; rc=$?
S2="$SNAPS/2026-09-20_0300"
check "second snapshot succeeds" "[ $rc -eq 0 ] && [ -d '$S2' ]"
check "unchanged file is HARD-LINKED (same inode: costs no extra space)" "[ \"\$(stat -c %i '$S1/brain/wiki/w.md')\" = \"\$(stat -c %i '$S2/brain/wiki/w.md')\" ]"
check "changed file has the new content in the new snapshot" "[ \"\$(cat '$S2/app/keep.txt')\" = changed ]"
check "...and the OLD snapshot still has the old content (real versioning)" "[ \"\$(cat '$S1/app/keep.txt')\" = keep ]"
check "'latest' moved" "[ \"\$(readlink '$SNAPS/latest')\" = 2026-09-20_0300 ]"

echo "== failure keeps the last good time"
LAST_OK_BEFORE="$(jq_py "d['last_ok']")"
echo x > "$APP/locked.txt"; chmod 000 "$APP/locked.txt"
run 2026-09-21_0300; rc=$?
chmod 600 "$APP/locked.txt"
check "an rsync error fails the run" "[ $rc -ne 0 ]"
check "status is ok=false but last_ok is PRESERVED" "[ \"\$(jq_py \"(not d['ok']) and d['last_ok'] == $LAST_OK_BEFORE\")\" = True ]"
check "the failed run left no snapshot and no .partial that looks finished" "[ ! -d '$SNAPS/2026-09-21_0300' ]"

echo "== rotation only touches snapshot-named folders"
rm -f "$APP/locked.txt"
mkdir -p "$SNAPS/2020-01-01_0000" "$SNAPS/2020-01-02_0000" "$SNAPS/important_notes"; echo precious > "$SNAPS/important_notes/keep.me"
mkdir -p "$SNAPS/2019-01-01_0000.partial"; touch -d '3 days ago' "$SNAPS/2019-01-01_0000.partial"
run 2026-09-22_0300 AURIX_BACKUP_KEEP=2; rc=$?
check "run succeeds with KEEP=2" "[ $rc -eq 0 ]"
check "only the newest 2 snapshots remain" "[ \"\$(ls '$SNAPS' | grep -cE '^[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{4}\$')\" = 2 ] && [ -d '$SNAPS/2026-09-22_0300' ] && [ -d '$SNAPS/2026-09-20_0300' ]"
check "old snapshots were pruned" "[ ! -e '$SNAPS/2020-01-01_0000' ] && [ ! -e '$SNAPS/2026-09-19_0300' ]"
check "an unrelated folder is NEVER touched" "[ \"\$(cat '$SNAPS/important_notes/keep.me')\" = precious ]"
check "a stale .partial from a crashed run is cleaned up" "[ ! -e '$SNAPS/2019-01-01_0000.partial' ]"

echo
echo "RESULT: $pass passed, $failn failed"
[ "$failn" -eq 0 ]
