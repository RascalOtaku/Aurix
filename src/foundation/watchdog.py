"""src/foundation/watchdog.py - AURIX tells YOU when its own immune system sees a problem.

Runs from Foundation.tick_standing() every few minutes (AURIX_WATCHDOG_MINUTES, default 5, 0 = off) on a
God's Eye snapshot. It only speaks up about things that need a human, once per problem (then again every 6 hours
while it persists, plus one line when it clears), so it cannot become another source of spam:

    audit chain broken            the hash-chained log was edited/truncated (critical)
    disk nearly full              >= 92 % used (an early heads-up at >= 85 %)
    memory tight                  RAM >= 92 % or swap >= 85 % used
    sandbox unreachable           only while a sandboxed mission is active or standing missions exist
    homelab service down          a homelab link that WAS answering stops (two checks in a row); one that never answered
                                  (e.g. a machine that is simply switched off) is never reported
    standing mission failing      2+ consecutive failed runs (it auto-pauses at 3)
    mission stuck                 the active mission has made no progress for 60+ minutes

and, once per server reboot, `boot_check()` says the server came back and what state it is in.

Pure decision logic in `evaluate()` (state in, alerts + new state out) so it is unit-testable without a clock or a
network. State persists in data/watchdog.json so a restart does not repeat old alerts.
"""
from __future__ import annotations

import html
import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.foundation import mission as ms

REALERT_SECONDS = 6 * 3600
DISK_PCT_LIMIT = 92.0
DISK_WARN_PCT = 85.0                     # early heads-up: 2026-09-19 a disk reached 96% with no warning at all
MEM_PCT_LIMIT = 92.0
SWAP_PCT_LIMIT = 85.0
STUCK_MINUTES = 60
SANDBOX_STRIKES = 2                      # consecutive checks before "sandbox down" / "homelab down" is believed
BACKUP_STALE_HOURS = 36.0                # the nightly backup (03:30) missed at least one night
BOOT_SETTLE_MINUTES = 3                  # after a reboot wait for the containers to come up before reporting
BOOT_JITTER_SECONDS = 600                # uptime is rounded to 0.1 h, so the computed boot time wobbles by minutes
e = html.escape


def state_path() -> Path:
    return ms.data_dir() / "watchdog.json"


def load_state() -> dict:
    try:
        raw = json.loads(state_path().read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def backup_status_path() -> Path:
    return ms.data_dir() / "backup_status.json"


def read_backup_status() -> Optional[dict]:
    """The nightly backup's report (written by scripts/aurix_backup.sh), or None if backups are not set up / unreadable."""
    try:
        raw = json.loads(backup_status_path().read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except (OSError, ValueError):
        return None


def backup_age_hours(status: Optional[dict], now: float) -> Optional[float]:
    last_ok = (status or {}).get("last_ok")
    return (now - last_ok) / 3600 if isinstance(last_ok, (int, float)) and not isinstance(last_ok, bool) else None


def interval_minutes() -> int:
    try:
        return max(0, int(os.environ.get("AURIX_WATCHDOG_MINUTES", "5")))
    except ValueError:
        return 5


def _problems(snapshot: dict, now: float) -> Dict[str, str]:
    """key -> human message for every problem that is present right now."""
    out: Dict[str, str] = {}
    organs = snapshot.get("organs", {})
    if not snapshot.get("audit", {}).get("ok", True):
        out["audit_tamper"] = ("🚨 <b>Audit chain is BROKEN</b> - the tamper-evident log was edited or truncated. "
                               "Treat this machine as suspect until you have looked. (<code>log</code> / <code>doctor</code>)")
    vitals = organs.get("circulatory", {}).get("metrics", {})
    disk = vitals.get("disk %")
    if isinstance(disk, (int, float)) and disk >= DISK_PCT_LIMIT:
        out["disk_low"] = f"🚨 Disk is {disk:.0f}% full on the AURIX host. Missions and logs will start failing. (<code>disk</code>)"
    elif isinstance(disk, (int, float)) and disk >= DISK_WARN_PCT:
        out["disk_getting_full"] = (f"💾 Disk is {int(disk)}% full on the AURIX host - worth freeing space before it hits "
                                    f"{DISK_PCT_LIMIT:.0f}%. (<code>disk</code>)")
    mem, swap = vitals.get("memory %"), vitals.get("swap %")
    if (isinstance(mem, (int, float)) and mem >= MEM_PCT_LIMIT) or (isinstance(swap, (int, float)) and swap >= SWAP_PCT_LIMIT):
        parts = [f"RAM {mem:.0f}%" if isinstance(mem, (int, float)) else "", f"swap {swap:.0f}%" if isinstance(swap, (int, float)) else ""]
        out["memory_tight"] = (f"⚠️ Memory is tight on the AURIX host ({', '.join(p for p in parts if p)} used). "
                               "AURIX slows down and may get OOM-killed. (<code>doctor</code>)")
    backup = snapshot.get("backup")
    if isinstance(backup, dict):                                # only when the owner has backups set up (a status file exists)
        age = backup_age_hours(backup, now)
        last_good = f"Last good backup: {age:.0f} h ago." if age is not None else "There has been no successful backup yet."
        if backup.get("ok") is False:
            out["backup_failed"] = f"💾 The nightly AURIX backup <b>failed</b>: {e(str(backup.get('message', 'unknown error'))[:200])}. {last_good}"
        elif age is not None and age >= BACKUP_STALE_HOURS:
            out["backup_stale"] = (f"💾 No successful AURIX backup for {age:.0f} hours - the nightly job (03:30) should have run. "
                                   "Is the NAS mounted? (<code>homelab</code>)")
    for row in snapshot.get("homelab") or []:
        if not row.get("ok") and not row.get("quiet") and (snapshot.get("_homelab_was_up") or {}).get(row.get("name")):
            out[f"homelab_down:{row['name']}"] = (f"🏠 Homelab: <b>{e(str(row['name']))}</b> stopped answering "
                                                  f"(<code>{e(str(row.get('url', '')))}</code>). It was up before.")
    sandbox = organs.get("muscular", {}).get("metrics", {}).get("sandbox")
    needs_sandbox = any(m.get("status") == "active" and m.get("sandboxed") for m in snapshot.get("missions", [])) \
        or any(s.get("status") == "active" for s in snapshot.get("standing", []))
    if sandbox == "UNREACHABLE" and needs_sandbox:
        out["sandbox_down"] = ("⚠️ The mission <b>sandbox is unreachable</b> while missions depend on it - nothing runs "
                               "(it fails closed, nothing unsafe). Check <code>docker compose ps</code>.")
    for s in snapshot.get("standing", []):
        if s.get("status") == "active" and s.get("failing", 0) >= 2:
            out[f"standing_failing:{s['id']}"] = (f"⚠️ Standing mission <code>{e(s['id'])}</code> ({e(s['title'])}) has failed "
                                                  f"{s['failing']} runs in a row; it auto-pauses at 3.")
    return out


def evaluate(snapshot: dict, state: dict, now: float, stuck_info: Optional[Tuple[str, float]] = None
             ) -> Tuple[List[str], dict]:
    """(messages to send, new state). `stuck_info` = (mission id, minutes since last progress) when active."""
    homelab_up = dict(state.get("homelab_up", {}))               # names that have answered at least once while we watched
    snapshot = dict(snapshot, _homelab_was_up=homelab_up)
    problems = _problems(snapshot, now)
    for row in snapshot.get("homelab") or []:
        if row.get("ok") and row.get("name"):
            homelab_up[str(row["name"])] = True
    if stuck_info and stuck_info[1] >= STUCK_MINUTES:
        problems[f"mission_stuck:{stuck_info[0]}"] = (
            f"⏳ Mission <code>{e(stuck_info[0])}</code> has made no progress for {int(stuck_info[1])} min. "
            "<code>status</code> to look, <code>resume</code> to nudge it, <code>stop</code> to halt.")
    seen = dict(state.get("seen", {}))
    strikes = dict(state.get("strikes", {}))
    messages: List[str] = []
    new_seen: Dict[str, dict] = {}

    for key, msg in problems.items():
        if key == "sandbox_down" or key.startswith("homelab_down:"):    # one failed probe is noise; two in a row is real
            strikes[key] = strikes.get(key, 0) + 1
            if strikes[key] < SANDBOX_STRIKES and key not in seen:
                continue
        prev = seen.get(key)
        if prev is None or now - prev.get("last_alert", 0) >= REALERT_SECONDS:
            messages.append(msg + ("" if prev is None else " <i>(still)</i>"))
            new_seen[key] = {"since": (prev or {}).get("since", now), "last_alert": now}
        else:
            new_seen[key] = prev
    for key in list(strikes):
        if key not in problems:
            strikes.pop(key)
    for key, prev in seen.items():                              # cleared since last time
        if key not in problems and prev.get("last_alert"):
            messages.append(f"✅ Cleared: <code>{e(key.split(':')[0])}</code>"
                            + (f" ({e(key.split(':', 1)[1])})" if ":" in key else ""))
    return messages, {"seen": new_seen, "strikes": strikes, "last_check": now,
                      "last_startup_notice": state.get("last_startup_notice", 0),
                      "homelab_up": homelab_up, "last_boot": state.get("last_boot")}


def boot_check(snapshot: dict, state: dict, now: float) -> Tuple[Optional[str], Optional[float]]:
    """(message or None, boot time to remember). Says once per SERVER reboot that it is back and how it looks.

    The boot time is derived from the host uptime. The first time it is seen it is only remembered (no message: an
    existing uptime is not news). A boot is reported after BOOT_SETTLE_MINUTES so the containers have started; until
    then the old value is kept, so the next check reports it."""
    vitals = snapshot.get("organs", {}).get("circulatory", {}).get("metrics", {})
    up_h = vitals.get("uptime h")
    last = state.get("last_boot")
    if not isinstance(up_h, (int, float)):
        return None, last
    boot_ts = now - up_h * 3600
    if last is None:
        return None, boot_ts
    if abs(boot_ts - last) <= BOOT_JITTER_SECONDS:
        return None, last
    if up_h * 60 < BOOT_SETTLE_MINUTES:
        return None, last
    from src.foundation import standing as st
    when = st.local_now(boot_ts).strftime("%a %H:%M")
    bad = [o.get("title", k) + f" ({o.get('status')})" for k, o in snapshot.get("organs", {}).items() if o.get("status") in ("warn", "bad")]
    facts = [f"{label} {vitals[key]:.0f}%" for label, key in (("disk", "disk %"), ("RAM", "memory %"), ("swap", "swap %"))
             if isinstance(vitals.get(key), (int, float))]
    text = (f"🔄 <b>The server rebooted</b> (up since {e(when)}) and AURIX is back. " + (" · ".join(facts) + ". " if facts else "")
            + (f"Needs a look: {e(', '.join(bad))}." if bad else "All systems normal."))
    return text, boot_ts


def stuck_info(store: Optional[ms.MissionStore] = None, now: Optional[float] = None) -> Optional[Tuple[str, float]]:
    """(active mission id, minutes since its file last changed) - the runner saves on every step and tool call."""
    store = store or ms.MissionStore()
    m = store.active()
    if m is None:
        return None
    try:
        age = ((now or time.time()) - (store.dir / f"{m.id}.json").stat().st_mtime) / 60
    except OSError:
        return None
    return m.id, age


def startup_text(store: Optional[ms.MissionStore] = None, running: bool = False) -> Optional[str]:
    """One line after a restart if a mission was mid-flight (its runner died with the process)."""
    m = (store or ms.MissionStore()).active()
    if m is None or running:
        return None
    return (f"🔄 AURIX restarted. Mission <code>{e(m.id)}</code> was in progress (step {m.current_step + 1}/{len(m.steps)}: "
            f"{e(m.objective[:80])}). Say <code>resume</code> to continue or <code>stop</code> to end it.")
