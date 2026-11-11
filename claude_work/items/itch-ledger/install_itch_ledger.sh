#!/usr/bin/env bash
# install_itch_ledger.sh: put AURIX's read-only itch.io sales tracker on this machine.
#
#   bash install_itch_ledger.sh            # install, check the key, ask before adding cron
#   bash install_itch_ledger.sh --yes      # same, no cron prompt
#   bash install_itch_ledger.sh --dry-run  # show what it would do, change nothing
#   bash install_itch_ledger.sh --uninstall  # remove the two cron lines (keeps the ledger)
#
# The key lives in its own file, $AURIX_HOME/config/itch.env (mode 600), not in vault.env,
# so nothing else on the box reads it. You paste the key there yourself; this script never
# prints it. It only reads sales numbers: no posting, pricing or payouts.
set -euo pipefail

AURIX_HOME="${AURIX_HOME:-$HOME/ai/brain}"
BIN="$AURIX_HOME/economic/itch_ledger.py"
ENV_FILE="$AURIX_HOME/config/itch.env"
LEDGER="$AURIX_HOME/ledger/earned.sqlite"
LOGS="$AURIX_HOME/logs"
MARK="# aurix-itch-ledger"
YES=0; DRY=0; UNINSTALL=0
for a in "$@"; do
  case "$a" in
    --yes) YES=1 ;; --dry-run) DRY=1 ;; --uninstall) UNINSTALL=1 ;;
    *) echo "unknown option: $a"; exit 2 ;;
  esac
done
run() { if [ "$DRY" = 1 ]; then echo "would: $*"; else "$@"; fi; }

RUNNER="cd $AURIX_HOME && set -a && . config/itch.env && set +a && AURIX_LEDGER_DB=$LEDGER python3 economic/itch_ledger.py"
CRON_SNAP="15 6 * * * $RUNNER snapshot >> $LOGS/itch_ledger.log 2>&1 $MARK"
CRON_WEEK="30 6 * * 0 $RUNNER week >> $LOGS/itch_week.log 2>&1 $MARK"

current_cron() { crontab -l 2>/dev/null || true; }

if [ "$UNINSTALL" = 1 ]; then
  if [ "$DRY" = 1 ]; then current_cron | grep -F "$MARK" | sed 's/^/would remove: /'; exit 0; fi
  existing="$(current_cron)"
  printf '%s\n' "$existing" | { grep -vF "$MARK" || true; } | crontab -
  echo "Removed the itch ledger cron lines. Ledger kept at $LEDGER."
  exit 0
fi

command -v python3 >/dev/null || { echo "python3 not found"; exit 1; }

# 1. Install the tracker script.
run mkdir -p "$AURIX_HOME/economic" "$AURIX_HOME/config" "$AURIX_HOME/ledger" "$LOGS"
if [ "$DRY" = 1 ]; then
  echo "would: write $BIN"
else
  cat > "$BIN" <<'AURIX_LEDGER_EOF'
#!/usr/bin/env python3
"""itch_ledger.py: read-only itch.io sales tracker for AURIX's earned-capital ledger.

Reads your own games' stats from the itch.io server API and records each run as a
snapshot in a ledger kept apart from personal accounts. It never posts, lists,
prices or pays anything. The only credential is a read-only API key.

Env:
  ITCH_API_KEY     itch.io API key (Settings > API keys). Required.
  AURIX_LEDGER_DB  ledger path (default ~/ai/brain/ledger/earned.sqlite)

Usage:
  itch_ledger.py snapshot          # fetch and store one snapshot (run from cron)
  itch_ledger.py week              # print the last 7 days: views, downloads, sales, earned
  itch_ledger.py check             # fetch once, write nothing, show how earnings are read
  itch_ledger.py snapshot --file games.json   # offline test with a saved API response
"""
import argparse, json, os, sqlite3, sys, time, urllib.error, urllib.request
from pathlib import Path

API = "https://api.itch.io/profile/games"
DB = Path(os.environ.get("AURIX_LEDGER_DB", "~/ai/brain/ledger/earned.sqlite")).expanduser()
TARGET_LOW, TARGET_HIGH = 100, 500  # cents per week


def fetch(file=None):
    if file:
        return json.loads(Path(file).read_text())
    key = os.environ.get("ITCH_API_KEY")
    if not key:
        sys.exit("ITCH_API_KEY not set")
    req = urllib.request.Request(API, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"itch.io refused the request (HTTP {e.code}); check ITCH_API_KEY")
    except urllib.error.URLError as e:
        sys.exit(f"could not reach itch.io: {e.reason}")


def db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS itch_snapshot(
        ts INTEGER, game_id INTEGER, title TEXT, views INTEGER, downloads INTEGER,
        purchases INTEGER, earned_cents INTEGER, PRIMARY KEY(ts, game_id))""")
    return c


def usd_cents(game):
    # earnings: [{currency, amount, amount_formatted}], amount in cents. Gross, before fees.
    return sum(int(e.get("amount", 0)) for e in game.get("earnings") or [] if e.get("currency") == "USD")


def snapshot(file=None):
    data = fetch(file)
    games = data.get("games", [])
    ts = int(time.time())
    with db() as c:
        for g in games:
            c.execute("INSERT OR REPLACE INTO itch_snapshot VALUES (?,?,?,?,?,?,?)",
                      (ts, g["id"], g.get("title", ""), g.get("views_count", 0),
                       g.get("downloads_count", 0), g.get("purchases_count", 0), usd_cents(g)))
    print(f"snapshot: {len(games)} game(s) at {ts}")


def check(file=None):
    """Fetch once and write nothing: confirms the key works and that 'amount' is in cents."""
    games = fetch(file).get("games", [])
    print(f"key OK: {len(games)} project(s) on this account")
    for g in games:
        shown = ", ".join(e.get("amount_formatted", "?") for e in g.get("earnings") or []) or "none yet"
        print(f"  {g.get('title','')}: {g.get('purchases_count',0)} sales; itch shows {shown}; "
              f"ledger would record ${usd_cents(g)/100:.2f}")
    print("If those two money figures differ, stop and fix usd_cents() before scheduling.")


def week(now=None):
    now = now or int(time.time())
    since = now - 7 * 86400
    c = db()
    rows = c.execute("""
        WITH first AS (SELECT game_id, MIN(ts) ts FROM itch_snapshot WHERE ts >= ? GROUP BY game_id),
             base AS (SELECT s.game_id, MAX(s.ts) ts FROM itch_snapshot s WHERE s.ts < ? GROUP BY s.game_id),
             last AS (SELECT game_id, MAX(ts) ts FROM itch_snapshot GROUP BY game_id)
        SELECT l.title,
               l.views - b.views, l.downloads - b.downloads,
               l.purchases - b.purchases, l.earned_cents - b.earned_cents
        FROM last lt JOIN itch_snapshot l ON l.game_id=lt.game_id AND l.ts=lt.ts
        JOIN (SELECT game_id, COALESCE((SELECT ts FROM base WHERE base.game_id=f.game_id), f.ts) ts FROM first f) bt
             ON bt.game_id=l.game_id
        JOIN itch_snapshot b ON b.game_id=bt.game_id AND b.ts=bt.ts""", (since, since)).fetchall()
    total = sum(r[4] for r in rows)
    for t, v, d, p, e in rows:
        print(f"{t}: +{v} views, +{d} downloads, +{p} sales, +${e/100:.2f}")
    status = "on target" if TARGET_LOW <= total <= TARGET_HIGH else ("above target" if total > TARGET_HIGH else "below target")
    print(f"WEEK: ${total/100:.2f} gross ({status}, target $1-$5)")
    return total


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["snapshot", "week", "check"])
    ap.add_argument("--file")
    a = ap.parse_args()
    {"snapshot": lambda: snapshot(a.file), "week": week, "check": lambda: check(a.file)}[a.cmd]()
AURIX_LEDGER_EOF
  chmod 755 "$BIN"
fi
echo "Tracker: $BIN"

# 2. The key file. Created empty if missing; you fill it in.
if [ ! -f "$ENV_FILE" ]; then
  if [ "$DRY" = 1 ]; then echo "would: create $ENV_FILE (mode 600) with an empty ITCH_API_KEY="
  else
    umask 077; printf '# itch.io API key (read-only use). Paste it after the = sign.\nITCH_API_KEY=\n' > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
  fi
fi
key_set() { [ -f "$ENV_FILE" ] && grep -qE '^ITCH_API_KEY=.+' "$ENV_FILE"; }
if ! key_set; then
  echo
  echo "Next: put your itch.io API key in $ENV_FILE"
  echo "  (itch.io > Settings > API keys > Generate new API key), e.g.  nano $ENV_FILE"
  echo "Then run this script again. Nothing scheduled yet."
  exit 0
fi
[ "$DRY" = 0 ] && chmod 600 "$ENV_FILE"

# 3. Check the key and how earnings are read, writing nothing.
if [ "$DRY" = 1 ]; then echo "would: run itch_ledger.py check"; else
  ( set -a; . "$ENV_FILE"; set +a; python3 "$BIN" check ) || { echo "Key check failed. Nothing scheduled."; exit 1; }
fi

# 4. First snapshot.
if [ "$DRY" = 1 ]; then echo "would: run itch_ledger.py snapshot"; else
  ( cd "$AURIX_HOME"; set -a; . "$ENV_FILE"; set +a; AURIX_LEDGER_DB="$LEDGER" python3 "$BIN" snapshot )
fi

# 5. Schedule: daily snapshot 06:15, weekly summary Sunday 06:30.
if current_cron | grep -qF "$MARK"; then
  echo "Cron lines already installed. Done."
  exit 0
fi
echo
echo "About to add these 2 lines to your crontab:"
echo "  $CRON_SNAP"
echo "  $CRON_WEEK"
if [ "$DRY" = 1 ]; then echo "(dry run: not added)"; exit 0; fi
if [ "$YES" = 0 ]; then
  read -r -p "Add them? [y/N] " ans
  case "$ans" in y|Y|yes) ;; *) echo "Skipped. Tracker installed, nothing scheduled."; exit 0 ;; esac
fi
existing="$(current_cron)"
{ [ -n "$existing" ] && printf '%s\n' "$existing"; echo "$CRON_SNAP"; echo "$CRON_WEEK"; } | crontab -
echo "Scheduled. Weekly totals land in $LOGS/itch_week.log on Sundays."
