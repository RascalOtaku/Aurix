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
