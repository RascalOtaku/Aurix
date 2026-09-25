"""src/foundation/gaming.py - AURIX's game doctor: ask about your Steam games and mods, get clear-cut fix proposals, answer yes or no.

The games live on the owner's Windows PC; a small agent there (gaming/agent/aurix_gaming_agent.py) writes a read-only fact report into the
synced folder gaming/ and applies ONLY what the owner approved. This module is the AURIX half:

  facts (report.json)  ->  analyze()  ->  issues  ->  proposals (a plain report with evidence, exact change, risk, undo)  ->  `yes g-xxxxxx`
  ->  a SIGNED approval file (HMAC; the PC verifies it) -> the PC applies it with a backup -> a result file -> I message you.

The model never decides anything here: detection is code, the allowed change types are a short fixed list, and the PC re-checks the
signature, the expiry, that the game is not running, and the allow-list. Everything is audited. A proposal that needs real hands-on
debugging (heap corruption, a crash inside the game or Windows itself) is reported as information with no yes/no, together with what AURIX
does next about it. A crash the log pins on one script-extender DLL becomes a yes/no fix that moves that one file out (undoable).
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.foundation import audit

APPROVAL_TTL_HOURS = 24
DECLINE_QUIET_DAYS = 30
INFO_REPEAT_DAYS = 7
MAX_NEW_PER_TICK = 3
LOW_DISK_GB = 20.0
DUMP_MB = 300
STALE_REPORT_HOURS = 3
LIGHT_LIMIT = 254

e = html.escape


def gaming_dir() -> Path:
    return Path(os.environ.get("AURIX_GAMING_DIR") or (Path(os.environ.get("AURIX_BRAIN", "/aurix")) / "gaming"))


def _data() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data" / "gaming"


def _atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------------------------------------------------------------
# signing (identical rule to the Windows agent; a test proves they agree)
# ---------------------------------------------------------------------------------------------------------------------------------

def canonical(payload: dict) -> bytes:
    body = {k: v for k, v in payload.items() if k != "sig"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def sign(key: bytes, payload: dict) -> str:
    return hmac.new(key, canonical(payload), hashlib.sha256).hexdigest()


def load_key() -> Optional[bytes]:
    raw = os.environ.get("AURIX_GAMING_HMAC_KEY", "").strip()
    try:
        return bytes.fromhex(raw) if raw else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------------------------------------------------------------
# the report and what is wrong in it
# ---------------------------------------------------------------------------------------------------------------------------------

def load_report() -> Optional[dict]:
    try:
        return json.loads((gaming_dir() / "report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def report_age_hours(rep: Optional[dict], now: Optional[float] = None) -> Optional[float]:
    if not rep or not rep.get("generated_ts"):
        return None
    return ((now or time.time()) - float(rep["generated_ts"])) / 3600


def broken_plugins(pl: dict) -> Dict[str, str]:
    """{plugin: why} for every enabled plugin that would certainly stop the game loading, and (repeatedly) everything that in turn
    depends on a plugin we would have to turn off.

    CONSERVATIVE ON PURPOSE (learned from the owner's real Skyrim load order, 2026-09-20): Vortex writes `.esl` files WITHOUT a star and
    leaves some masters unlisted, and the game loads them fine, so "installed but not starred" does NOT mean switched off for .esl/.esm.
    A master is only ever reported when (a) its file is not installed at all, (b) it is an .esp that is not enabled, or (c) it is an
    .esp/.esm that plugins.txt explicitly lists as switched off. Everything ambiguous is left alone: a false alarm here would have the
    owner switch off half a load order."""
    implicit = {x.lower() for x in pl.get("implicit", [])}
    present = {x.lower() for x in pl.get("present", [])}
    enabled = list(pl.get("enabled", []))
    off = {x.lower() for x in pl.get("disabled", []) if not x.lower().endswith(".esl")}
    masters = {k: list(v) for k, v in (pl.get("masters") or {}).items()}
    on = {n.lower() for n in enabled} | implicit
    broken: Dict[str, str] = {}
    changed = True
    while changed:
        changed = False
        for name in enabled:
            if name in broken or name.lower() in implicit:
                continue
            for m in masters.get(name, []):
                ml = m.lower()
                if ml in implicit:
                    continue
                if ml not in present:
                    why = f"needs {m}, which is not installed"
                elif ml in broken_lower(broken):
                    why = f"needs {m}, which has to be switched off too"
                elif ml in off or (ml.endswith(".esp") and ml not in on):
                    why = f"needs {m}, which is installed but switched off"
                else:
                    continue
                broken[name] = why
                on.discard(name.lower())
                changed = True
                break
    return broken


def broken_lower(broken: Dict[str, str]) -> set:
    return {k.lower() for k in broken}


# Crash modules that are the game or Windows itself: a crash "in" one of these does not name a mod.
_SYSTEM_MODULES = {"ntdll.dll", "kernelbase.dll", "kernel32.dll", "ucrtbase.dll", "msvcrt.dll", "vcruntime140.dll", "msvcp140.dll", "d3d11.dll", "dxgi.dll",
                   "nvwgf2umx.dll", "atidxx64.dll", "nvlddmkm.sys", "win32u.dll", "user32.dll", "unknown"}
_ENGINE_NOTE = ("The crash points at the game or Windows itself ({mod}), not at one mod, so there is no single file I can safely switch off. "
                "I keep watching: the moment one mod's DLL is named in two crashes I will offer to move it out with a yes/no (undoable).")


def crash_note(top_module: str, has_dll_dir: bool) -> str:
    """What AURIX itself does about a crash it cannot pin on one mod file. Never hands the problem to someone else."""
    mod = top_module or "unknown"
    if mod.lower() in _SYSTEM_MODULES or mod.lower().endswith(".exe"):
        return _ENGINE_NOTE.format(mod=mod)
    if not has_dll_dir:
        return (f"The crash names {mod}, but I cannot see inside this game's mod folders yet, so I will not guess which file to touch. "
                "Your load order is checked separately and I keep watching for a repeat.")
    return (f"{mod} is not a script-extender plugin I can find in this game's plugin folder, so I cannot switch it off safely. "
            "I keep watching: if a plugin DLL is named in two crashes I will offer to move it out with a yes/no (undoable).")


def resolved_crash_modules() -> set:
    """{(appid, module)} for mod DLLs you already had moved out: their old crash records are history, not a live problem."""
    out = set()
    for p in all_proposals():
        if p.get("kind") == "crashing_mod" and p.get("status") == "applied":
            for a in (p.get("fix") or {}).get("actions", []):
                if a.get("type") == "quarantine_dll":
                    out.add((str(p.get("appid")), str(a.get("dll", "")).lower()))
    return out


def analyze(report: dict, resolved: Optional[set] = None) -> List[dict]:
    """Issues found in a report. Pure and deterministic. `fix` is None for things that need a hands-on session.
    `resolved` = {(appid, module)} already fixed; crash records naming them are ignored."""
    resolved = resolved or set()
    issues: List[dict] = []
    days_ago = lambda iso: (time.time() - datetime.fromisoformat(iso).timestamp()) / 86400   # noqa: E731
    for g in report.get("games", []):
        name, appid = g.get("name", "?"), str(g.get("appid", ""))
        pl = g.get("plugins")
        if pl:
            bad = broken_plugins(pl)
            if bad:
                ev = [f"{n} {why}" for n, why in list(bad.items())[:6]] + ([f"...and {len(bad) - 6} more"] if len(bad) > 6 else [])
                issues.append({"key": f"{appid}:missing_master:" + hashlib.sha1("|".join(sorted(bad)).encode()).hexdigest()[:8], "game": name,
                               "appid": appid, "kind": "missing_master", "severity": "high",
                               "title": f"{len(bad)} {name} plugin{'s' if len(bad) != 1 else ''} would crash the game at startup", "evidence": ev,
                               "fix": {"actions": [{"type": "plugins_disable", "plugins": sorted(bad)}],
                                       "summary": "Switch these plugins off in plugins.txt (nothing is deleted): " + ", ".join(sorted(bad)[:8])
                                       + (" ..." if len(bad) > 8 else ""),
                                       "risk": "Low. A backup of plugins.txt is kept on your PC, no mod files or saves are touched. A save that used one of "
                                               "these plugins may complain, but the game was going to crash anyway. Undo restores it exactly."}})
            non_light = [n for n in pl.get("enabled", []) if n not in set(pl.get("light", []))]
            if len(non_light) > LIGHT_LIMIT:
                issues.append({"key": f"{appid}:plugin_limit", "game": name, "appid": appid, "kind": "plugin_limit", "severity": "warn",
                               "title": f"{name} has {len(non_light)} full plugins enabled (the engine limit is {LIGHT_LIMIT + 1})",
                               "evidence": ["Too many enabled full plugins makes the game crash or lose load-order slots."], "fix": None})
        ev = [x for x in g.get("crash_events", []) if x.get("when") and days_ago(x["when"]) <= 14 and (appid, str(x.get("module", "")).lower()) not in resolved]
        if ev:
            mods: Dict[str, int] = {}
            shown: Dict[str, str] = {}
            for x in ev:                                                     # Windows is not consistent about case in module names
                m = str(x.get("module", "?"))
                shown.setdefault(m.lower(), m)
                mods[shown[m.lower()]] = mods.get(shown[m.lower()], 0) + 1
            top = sorted(mods.items(), key=lambda t: -t[1])
            have = {x.lower(): x for x in (g.get("mod_dlls") or [])}
            culprit = next(((have[m.lower()], n) for m, n in top if m.lower() in have and m.lower() not in _SYSTEM_MODULES and n >= 2), None)
            last = f"most recent: {max(x['when'] for x in ev)[:16].replace('T', ' ')}"
            if culprit:
                dll, n = culprit
                issues.append({"key": f"{appid}:crashing_mod:{dll.lower()}", "game": name, "appid": appid, "kind": "crashing_mod", "severity": "high",
                               "title": f"{name} keeps crashing inside one mod: {dll}",
                               "evidence": [f"the crash log names {dll} in {n} of {len(ev)} crashes", last] + [f"other crashing module: {m} ({c}x)" for m, c in top[:3] if m.lower() != dll.lower()],
                               "fix": {"actions": [{"type": "quarantine_dll", "dll": dll}],
                                       "summary": f"Move {dll} out of the game's plugin folder into quarantine (nothing is deleted).",
                                       "risk": "Low. The mod that needs it will simply not load its script-extender part; mods that require it may complain in game. "
                                               "Undo puts the file back exactly. If Vortex re-adds it on the next Deploy, disable that mod in Vortex instead."}})
            else:
                issues.append({"key": f"{appid}:crashes:" + top[0][0].lower(), "game": name, "appid": appid, "kind": "recent_crashes", "severity": "warn",
                               "title": f"{name} crashed {len(ev)} time{'s' if len(ev) != 1 else ''} in the last 14 days",
                               "evidence": [f"crashing module: {m} ({n}x)" for m, n in top[:3]] + [last],
                               "fix": None, "hands_on": crash_note(top[0][0], bool(g.get("mod_dlls") is not None))})
        d = g.get("dumps") or {}
        if d.get("bytes", 0) >= DUMP_MB * 1e6 and d.get("oldest_days", 0) >= 7:
            issues.append({"key": f"{appid}:dumps", "game": name, "appid": appid, "kind": "crash_dumps", "severity": "info",
                           "title": f"{name}: {round(d['bytes'] / 1e6)} MB of old crash dumps",
                           "evidence": [f"{d['count']} dump files, oldest {d['oldest_days']} days"],
                           "fix": {"actions": [{"type": "delete_dumps", "older_than_days": 7}], "summary": "Move dumps older than 7 days to quarantine.",
                                   "risk": "None to the game. They are moved, not deleted, and can be restored with undo."}})
    for lib in report.get("libraries", []):
        if lib.get("free_gb", 1e9) < LOW_DISK_GB:
            issues.append({"key": f"disk:{lib.get('path')}", "game": "Steam", "appid": "", "kind": "low_disk", "severity": "warn",
                           "title": f"Steam library {lib.get('path')} has only {lib.get('free_gb')} GB free", "evidence": ["Updates and shader caches need room."], "fix": None})
    return issues


# ---------------------------------------------------------------------------------------------------------------------------------
# proposals
# ---------------------------------------------------------------------------------------------------------------------------------

def _pdir() -> Path:
    return _data() / "proposals"


def _ppath(pid: str) -> Path:
    return _pdir() / f"{pid}.json"


def load_proposal(pid: str) -> Optional[dict]:
    if not re.fullmatch(r"g-[0-9a-f]{6}", pid or ""):
        return None
    try:
        return json.loads(_ppath(pid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def all_proposals() -> List[dict]:
    out = []
    try:
        for p in sorted(_pdir().glob("g-*.json")):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except ValueError:
                continue
    except OSError:
        pass
    return sorted(out, key=lambda x: x.get("created", 0))


def save_proposal(p: dict) -> None:
    _atomic(_ppath(p["id"]), p)


def pending() -> List[dict]:
    return [p for p in all_proposals() if p.get("status") == "pending"]


def _suppressed(key: str, kind: str, now: float) -> bool:
    for p in all_proposals():
        if p.get("key") != key:
            continue
        st, age = p.get("status"), (now - p.get("created", 0)) / 86400
        if st in ("pending", "approved"):
            return True
        if st == "declined" and age < DECLINE_QUIET_DAYS:
            return True
        if st in ("applied", "undone") and age < 7:
            return True
        if st == "info" and age < INFO_REPEAT_DAYS:
            return True
    return False


def sync_proposals(issues: List[dict], now: Optional[float] = None) -> List[dict]:
    """New proposals for issues not already handled. Fixable issues become `pending` (yes/no); the rest are `info` reports."""
    now = now or time.time()
    fresh = []
    for old in all_proposals():                          # notes written before 9/20 told you to take the crash to someone else; AURIX owns it now
        note = str(old.get("hands_on", ""))
        if "Claude Code" in note or "the module named above" in note:
            mod = next((re.match(r"crashing module: (.+?) \(", x).group(1) for x in old.get("evidence", []) if re.match(r"crashing module: (.+?) \(", str(x))), "")
            fix = next((p for p in all_proposals() if p.get("kind") == "crashing_mod" and p.get("appid") == old.get("appid") and p.get("status") in ("pending", "approved", "applied")), None)
            if fix:
                old["status"], old["hands_on"] = "superseded", f"Replaced by fix {fix['id']}, which names the mod file."
            else:
                old["hands_on"] = crash_note(mod, True)
            save_proposal(old)
    for iss in issues:
        if _suppressed(iss["key"], iss["kind"], now):
            continue
        p = {"id": "g-" + secrets.token_hex(3), "key": iss["key"], "game": iss["game"], "appid": iss["appid"], "kind": iss["kind"],
             "severity": iss["severity"], "title": iss["title"], "evidence": iss["evidence"], "fix": iss.get("fix"),
             "hands_on": iss.get("hands_on", ""), "status": "pending" if iss.get("fix") else "info", "created": now}
        save_proposal(p)
        audit.append("gaming_proposal", id=p["id"], game=p["game"], kind=p["kind"], status=p["status"])
        fresh.append(p)
    return fresh


def render_proposal(p: dict) -> str:
    sev = {"high": "🔴", "warn": "🟠", "info": "🔵"}.get(p.get("severity"), "•")
    lines = [f"🎮 {sev} <b>{e(p['title'])}</b>  <code>{p['id']}</code>", f"<b>Game:</b> {e(p['game'])}", "<b>What I found:</b>"]
    lines += [f"• {e(x)}" for x in p["evidence"][:7]]
    if p.get("fix"):
        lines += ["<b>What I will change:</b>", e(p["fix"]["summary"]), f"<b>Risk:</b> {e(p['fix']['risk'])}",
                  f"<b>Your call:</b>  <code>yes {p['id']}</code>   or   <code>no {p['id']}</code>   <i>(undo later with <code>undo {p['id']}</code>)</i>"]
    else:
        lines.append("<b>What now:</b> " + e(p.get("hands_on") or "Nothing I can fix safely on my own; this is for your information."))
    return "\n".join(lines)


def render_pending() -> str:
    ps = pending()
    if not ps:
        return "No game fixes are waiting on you. <code>games</code> shows what I know; <code>games refresh</code> asks the PC for fresh facts."
    return "\n\n".join(render_proposal(p) for p in ps[:5]) + (f"\n\n<i>...and {len(ps) - 5} more.</i>" if len(ps) > 5 else "")


def approve(pid: str, now: Optional[float] = None) -> str:
    p = load_proposal(pid)
    if p is None:
        return f"No game fix {e(pid)}."
    if p["status"] != "pending" or not p.get("fix"):
        return f"Fix {pid} is {p['status']}; nothing to approve."
    key = load_key()
    if key is None:
        return "I cannot sign an approval: AURIX_GAMING_HMAC_KEY is not set on the server."
    now = now or time.time()
    item = {"id": p["id"], "kind": "apply", "game": p["game"], "appid": p["appid"], "actions": p["fix"]["actions"],
            "created": datetime.fromtimestamp(now, timezone.utc).astimezone().isoformat(timespec="seconds"),
            "expires": (datetime.fromtimestamp(now, timezone.utc) + timedelta(hours=APPROVAL_TTL_HOURS)).astimezone().isoformat(timespec="seconds")}
    item["sig"] = sign(key, item)
    _atomic(gaming_dir() / "approved" / f"{p['id']}.json", item)
    p["status"], p["decided"] = "approved", now
    save_proposal(p)
    audit.append("gaming_fix_approved", id=p["id"], game=p["game"], actions=[a["type"] for a in p["fix"]["actions"]])
    return f"✅ Approved <code>{p['id']}</code>. Your PC applies it within a few minutes (it must be on and the game closed) and I will message you the result."


def decline(pid: str, now: Optional[float] = None) -> str:
    p = load_proposal(pid)
    if p is None:
        return f"No game fix {e(pid)}."
    if p["status"] not in ("pending", "info"):
        return f"Fix {pid} is {p['status']}."
    p["status"], p["decided"] = "declined", now or time.time()
    save_proposal(p)
    audit.append("gaming_fix_declined", id=pid)
    return f"Declined <code>{pid}</code>. I will not raise this same issue again for {DECLINE_QUIET_DAYS} days."


def undo(pid: str, now: Optional[float] = None) -> str:
    p = load_proposal(pid)
    if p is None or p["status"] != "applied":
        return f"Fix {e(pid)} was not applied, so there is nothing to undo."
    key = load_key()
    if key is None:
        return "I cannot sign an undo: AURIX_GAMING_HMAC_KEY is not set on the server."
    now = now or time.time()
    item = {"id": p["id"], "kind": "undo", "target": p["id"], "appid": p["appid"],
            "created": datetime.fromtimestamp(now, timezone.utc).astimezone().isoformat(timespec="seconds"),
            "expires": (datetime.fromtimestamp(now, timezone.utc) + timedelta(hours=APPROVAL_TTL_HOURS)).astimezone().isoformat(timespec="seconds")}
    item["sig"] = sign(key, item)
    _atomic(gaming_dir() / "approved" / f"undo-{p['id']}.json", item)
    audit.append("gaming_fix_undo_requested", id=p["id"])
    return f"↩️ Undo requested for <code>{p['id']}</code>. Your PC restores the backup within a few minutes and I will confirm."


def ingest_results() -> List[str]:
    """Read result files the PC wrote; update proposals; return the owner messages. Each result is processed once."""
    out: List[str] = []
    rdir = gaming_dir() / "results"
    seen_path = _data() / "seen_results.json"
    try:
        seen = set(json.loads(seen_path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        seen = set()
    for f in sorted(rdir.glob("*.json")) if rdir.exists() else []:
        if f.name in seen:
            continue
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        seen.add(f.name)
        p = load_proposal(str(r.get("id", "")))
        if p is None:
            continue
        detail = r.get("detail")
        text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)[:400]
        if r.get("kind") == "undo":
            p["status"] = "undone" if r.get("status") == "undone" else "applied"
            out.append(("↩️ <b>Undone</b>" if r.get("status") == "undone" else "⚠️ <b>Undo failed</b>") + f" <code>{p['id']}</code> - {e(text[:300])}")
        elif r.get("status") == "applied":
            p["status"] = "applied"
            out.append(f"✅ <b>Fixed</b> <code>{p['id']}</code> - {e(p['title'])}\n<i>{e(text[:300])}</i>\nTo take it back: <code>undo {p['id']}</code>")
        else:
            p["status"] = "failed"
            out.append(f"⚠️ <b>Not applied</b> <code>{p['id']}</code> - {e(str(r.get('status')))}: {e(text[:300])}")
        p["result"] = r
        save_proposal(p)
        audit.append("gaming_fix_result", id=p["id"], status=r.get("status"), kind=r.get("kind", "apply"))
    _atomic(seen_path, sorted(seen))
    return out


def request_refresh() -> str:
    d = gaming_dir() / "requests"
    d.mkdir(parents=True, exist_ok=True)
    (d / "collect-now").write_text(str(time.time()), encoding="utf-8")
    return "🔄 Asked your PC for fresh facts. It answers within a few minutes (it has to be on); then <code>games</code> is up to date."


# ---------------------------------------------------------------------------------------------------------------------------------
# reading it back to the owner
# ---------------------------------------------------------------------------------------------------------------------------------

def _match(rep: dict, query: str) -> Optional[dict]:
    q = re.sub(r"[^a-z0-9 ]", "", (query or "").lower()).strip()
    if not q:
        return None
    best, best_score = None, 0
    for g in rep.get("games", []):
        n = re.sub(r"[^a-z0-9 ]", "", g.get("name", "").lower())
        score = 3 if q == n else 2 if q in n else 1 if any(w and w in n for w in q.split()) else 0
        if score > best_score:
            best, best_score = g, score
    return best


def games_text(now: Optional[float] = None) -> str:
    rep = load_report()
    if rep is None:
        return ("I have no report from your PC yet. The agent on the PC writes one every ~30 minutes once it is installed; "
                "<code>games refresh</code> asks for one now.")
    age = report_age_hours(rep, now)
    n_pending = {}
    for p in pending():
        n_pending[p["game"]] = n_pending.get(p["game"], 0) + 1
    lines = [f"🎮 <b>Your Steam games</b> ({len(rep.get('games', []))}) - report {'%.0f' % (age * 60) + ' min' if age is not None and age < 2 else ('%.1f' % age + ' h') if age is not None else '?'} old"
             + (" ⚠️ <i>stale: is the PC on?</i>" if age is not None and age > STALE_REPORT_HOURS else "")]
    for g in sorted(rep.get("games", []), key=lambda x: -x.get("size_gb", 0)):
        pl = g.get("plugins")
        bits = [f"{g.get('size_gb', 0)} GB"]
        if pl:
            bits.append(f"{len(pl['enabled'])} plugins on")
        if g.get("crash_events"):
            bits.append(f"{len(g['crash_events'])} crash(es)")
        if n_pending.get(g.get("name")):
            bits.append(f"{n_pending[g['name']]} fix waiting")
        lines.append(f"• <b>{e(g.get('name', '?'))}</b> - " + ", ".join(bits))
    for lib in rep.get("libraries", []):
        lines.append(f"💽 {e(lib.get('path', ''))}: {lib.get('free_gb')} GB free of {lib.get('total_gb')}")
    lines.append("<i><code>game skyrim</code> for one game · <code>fixes</code> for what needs your yes/no · <code>games refresh</code></i>")
    return "\n".join(lines)


def game_text(query: str) -> str:
    rep = load_report()
    if rep is None:
        return games_text()
    g = _match(rep, query)
    if g is None:
        return f"I do not see a game matching '{e(query)}'. <code>games</code> lists them."
    lines = [f"🎮 <b>{e(g['name'])}</b> - {g.get('size_gb')} GB, build {e(str(g.get('build')))}, updated "
             f"{datetime.fromtimestamp(g['updated']).strftime('%Y-%m-%d') if g.get('updated') else '?'}"]
    pl = g.get("plugins")
    if pl:
        bad = broken_plugins(pl)
        lines.append(f"<b>Mods (plugins):</b> {len(pl['enabled'])} enabled of {len(pl['present'])} installed"
                     + (f" - <b>{len(bad)} would break loading</b>" if bad else " - all masters present"))
        lines.append("<i>" + e(", ".join(pl["enabled"][-8:])) + "</i> (last loaded)")
    elif not g.get("known"):
        lines.append("<i>I only track install facts for this one (no mod analysis).</i>")
    for x in (g.get("crash_events") or [])[:4]:
        lines.append(f"💥 {e(x.get('when', '')[:16].replace('T', ' '))} - {e(x.get('module', '?'))} ({e(x.get('code', ''))})")
    mine = [p for p in all_proposals() if p.get("game") == g["name"] and p.get("status") in ("pending", "info")]
    for p in mine[:3]:
        lines.append(f"{'🔧' if p['status'] == 'pending' else 'ℹ️'} {e(p['title'])} <code>{p['id']}</code>")
    return "\n".join(lines)


def summary_for_model(rep: dict, limit: int = 6000) -> str:
    rows = []
    for g in rep.get("games", []):
        pl = g.get("plugins")
        row = {"name": g.get("name"), "size_gb": g.get("size_gb"), "build": g.get("build")}
        if pl:
            row.update({"plugins_enabled": len(pl["enabled"]), "plugins_installed": len(pl["present"]), "broken": broken_plugins(pl),
                        "last_loaded": pl["enabled"][-6:]})
        if g.get("crash_events"):
            row["crashes"] = [(x["when"][:10], x["module"]) for x in g["crash_events"][:5]]
        rows.append(row)
    return json.dumps({"games": rows, "libraries": rep.get("libraries")}, ensure_ascii=False)[:limit]


async def ask(question: str, llm: Optional[Callable]) -> str:
    """Answer a free-form question about the games from the report ONLY."""
    rep = load_report()
    if rep is None:
        return games_text()
    if llm is None:
        return games_text()
    system = ("You answer questions about the owner's Steam games and mods using ONLY the JSON report below. If the answer is not in it, say "
              "so and suggest `games refresh` or `game <name>`. Be brief and concrete. Never invent mods or versions. Do not suggest running "
              "commands; the owner replies yes/no to fix proposals that AURIX sends.\n\nREPORT:\n" + summary_for_model(rep))
    try:
        return e((await llm(system, question)).strip()[:1800])
    except Exception:
        return games_text()


def digest_line() -> str:
    n = len(pending())
    return f"<b>Games:</b> {n} fix{'es' if n != 1 else ''} waiting for your yes/no (<code>fixes</code>)" if n else ""


# ---------------------------------------------------------------------------------------------------------------------------------
# the periodic tick (called by the Foundation scheduler pass)
# ---------------------------------------------------------------------------------------------------------------------------------

def tick(now: Optional[float] = None) -> List[str]:
    """Messages to send the owner: results of applied fixes, and new proposals from a fresh report. Cheap when nothing changed."""
    now = now or time.time()
    msgs = ingest_results()
    rep = load_report()
    if rep is not None:
        state_path = _data() / "state.json"
        try:
            last = json.loads(state_path.read_text(encoding="utf-8")).get("generated_ts", 0)
        except (OSError, ValueError):
            last = 0
        if rep.get("generated_ts", 0) != last:
            fresh = sync_proposals(analyze(rep, resolved_crash_modules()), now)
            _atomic(state_path, {"generated_ts": rep.get("generated_ts", 0)})
            for p in fresh[:MAX_NEW_PER_TICK]:
                msgs.append(render_proposal(p))
            if len(fresh) > MAX_NEW_PER_TICK:
                msgs.append(f"<i>{len(fresh) - MAX_NEW_PER_TICK} more game notes: send <code>fixes</code>.</i>")
    return msgs
