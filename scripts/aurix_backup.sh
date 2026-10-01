#!/usr/bin/env bash
# aurix_backup.sh - nightly, versioned backup of AURIX's own state to the NAS (/mnt/hearthnode/aurix-backup).
#
# Written 2026-09-19 after Timeshift was removed from the 7070: nothing versioned protected AURIX's live state any more.
#
# Safe by design:
#   * It REFUSES to run unless the NAS is really mounted. Otherwise it would write into the empty local mount point
#     and quietly fill the 7070's own disk. (The NFS line in fstab is `nofail`, so a Pi that is off must not be fatal.)
#   * It refuses if the marker folder <NAS>/aurix-backup is missing - creating that folder on the NAS is the opt-in.
#   * Snapshots are hard-linked to the previous one (rsync --link-dest), so a night with no changes costs almost nothing.
#     Each snapshot is built as <stamp>.partial and renamed only when complete, so a crash never leaves a half backup
#     that looks good. The newest KEEP (default 14) snapshots are kept.
#   * SQLite databases are copied with SQLite's own backup API (consistent even while the app writes) and integrity-checked.
#   * Secrets are never copied: .env, auth.json, ssh/, *.pem, *.key, credentials*. Big re-creatable stuff is skipped:
#     fastembed_cache (a downloaded model), emoji_cache, and mission workspaces (deliver those with `send files`).
#   * It writes <app data>/backup_status.json; the AURIX watchdog messages the owner if the last good backup is old or failed.
#
# Restore: copy what you need back from <NAS>/aurix-backup/snapshots/<stamp>/{brain,app}/ (brain -> ~/ai/brain, app -> odysseus/data).
# Run by cron:   30 3 * * *  bash ~/ai-brain-sync/odysseus/scripts/aurix_backup.sh
# Test knobs (env): AURIX_BACKUP_NAS, AURIX_BRAIN_DIR, AURIX_APP_DATA, AURIX_BACKUP_KEEP, AURIX_BACKUP_STATUS, AURIX_BACKUP_LOG,
#                   AURIX_BACKUP_REQUIRE_MOUNT=0 (tests only: skips the mount check so a temp dir can stand in for the NAS).
set -uo pipefail

NAS="${AURIX_BACKUP_NAS:-/mnt/hearthnode}"
ROOT="$NAS/aurix-backup"
SNAPS="$ROOT/snapshots"
BRAIN="${AURIX_BRAIN_DIR:-$HOME/ai/brain}"
APP="${AURIX_APP_DATA:-$HOME/ai-brain-sync/odysseus/data}"
KEEP="${AURIX_BACKUP_KEEP:-14}"
REQUIRE_MOUNT="${AURIX_BACKUP_REQUIRE_MOUNT:-1}"
STATUS="${AURIX_BACKUP_STATUS:-$APP/backup_status.json}"
LOG="${AURIX_BACKUP_LOG:-$HOME/aurix_backup.log}"
STAMP="${AURIX_BACKUP_STAMP:-$(date +%Y-%m-%d_%H%M)}"      # override is for tests only (so they need not wait a minute between runs)
BRAIN_ITEMS=(memory wiki .chroma runtime)
[[ "$STAMP" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{4}$ ]] || { echo "bad AURIX_BACKUP_STAMP: $STAMP" >&2; exit 2; }

log() { printf '%s %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOG" >&2; }

write_status() {   # $1 = 1|0 (ok), $2 = message, $3 = snapshot name
  python3 - "$STATUS" "$1" "$2" "${3:-}" <<'PY' 2>/dev/null || true
import json, os, sys, time
path, ok, msg, snap = sys.argv[1], sys.argv[2] == "1", sys.argv[3], sys.argv[4]
try:
    prev = json.load(open(path))
except Exception:
    prev = {}
now = time.time()
out = {"ok": ok, "message": msg[:300], "snapshot": snap, "finished": now,
       "finished_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "last_ok": now if ok else prev.get("last_ok")}
tmp = path + ".tmp"
json.dump(out, open(tmp, "w"))
os.replace(tmp, path)
PY
}

fail() { log "FAILED: $*"; write_status 0 "$*"; exit 1; }

[ -d "$BRAIN" ] || fail "brain directory missing: $BRAIN"
[ -d "$APP" ]   || fail "app data directory missing: $APP"
if [ "$REQUIRE_MOUNT" = "1" ]; then
  mountpoint -q "$NAS" || fail "the NAS is not mounted at $NAS - refusing to write into the local mount point"
fi
[ -d "$ROOT" ] || fail "marker folder missing on the NAS: $ROOT (create it to enable backups)"

exec 9>"${TMPDIR:-/tmp}/aurix_backup.lock"
flock -n 9 || { log "another backup is already running; exiting"; exit 0; }

mkdir -p "$SNAPS" || fail "cannot create $SNAPS"
find "$SNAPS" -maxdepth 1 -name '*.partial' -mmin +720 -exec rm -rf {} + 2>/dev/null   # leftovers of a crashed run

LATEST="$(ls -1d "$SNAPS"/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]_[0-9][0-9][0-9][0-9] 2>/dev/null | tail -1 || true)"
NEW="$SNAPS/$STAMP.partial"
FINAL="$SNAPS/$STAMP"
[ -e "$FINAL" ] && fail "snapshot $STAMP already exists (two runs in the same minute?)"
rm -rf "$NEW"
mkdir -p "$NEW/brain" "$NEW/app" || fail "cannot create $NEW"
log "backup start: brain=$BRAIN app=$APP -> $FINAL (previous: ${LATEST:-none})"

for d in "${BRAIN_ITEMS[@]}"; do
  [ -d "$BRAIN/$d" ] || continue
  LD=()
  [ -n "$LATEST" ] && [ -d "$LATEST/brain/$d" ] && LD=(--link-dest="$LATEST/brain/$d")
  rsync -a --delete "${LD[@]}" "$BRAIN/$d/" "$NEW/brain/$d/" || fail "rsync failed for brain/$d"
done

LD=()
[ -n "$LATEST" ] && [ -d "$LATEST/app" ] && LD=(--link-dest="$LATEST/app")
rsync -a --delete "${LD[@]}" \
  --exclude='fastembed_cache/' --exclude='emoji_cache/' --exclude='workspace/' \
  --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' --exclude='*.sqlite' --exclude='*.sqlite3' \
  --exclude='.env' --exclude='*.env' --exclude='.env.*' --exclude='auth.json' --exclude='ssh/' \
  --exclude='*.pem' --exclude='*.key' --exclude='credentials*' --exclude='*.lock' --exclude='*.tmp' --exclude='backup_status.json' \
  "$APP/" "$NEW/app/" || fail "rsync failed for app data"

DBS="$(python3 - "$APP" "$NEW/app" <<'PY'
import glob, os, sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
count = 0
for path in sorted(glob.glob(os.path.join(src, "*.db"))):
    out = os.path.join(dst, os.path.basename(path))
    s = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    d = sqlite3.connect(out)
    with d:
        s.backup(d)
    verdict = d.execute("PRAGMA integrity_check").fetchone()[0]
    s.close(); d.close()
    if verdict != "ok":
        print(f"INTEGRITY-FAILED {os.path.basename(path)}: {verdict}")
        sys.exit(2)
    count += 1
print(count)
PY
)" || fail "SQLite backup failed: $DBS"

mv "$NEW" "$FINAL" || fail "could not finalize $FINAL"
ln -sfn "$STAMP" "$SNAPS/latest" 2>/dev/null || true

# keep the newest $KEEP snapshots; only ever remove directories whose name is exactly a snapshot stamp under $SNAPS
mapfile -t ALL < <(ls -1 "$SNAPS" 2>/dev/null | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{4}$' | sort)
if [ "${#ALL[@]}" -gt "$KEEP" ]; then
  for old in "${ALL[@]:0:${#ALL[@]}-KEEP}"; do
    [[ "$old" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{4}$ ]] && rm -rf -- "$SNAPS/$old" && log "pruned old snapshot $old"
  done
fi

FILES="$(find "$FINAL" -type f | wc -l)"
log "backup OK: $STAMP ($FILES files, $DBS database(s) copied consistently); snapshots kept: $(ls -1 "$SNAPS" | grep -cE '^[0-9]{4}-')"
write_status 1 "ok: $FILES files, $DBS database(s)" "$STAMP"
tail -n 500 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
exit 0
