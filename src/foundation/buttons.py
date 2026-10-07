"""src/foundation/buttons.py - tap instead of type.

Every message AURIX sends that expects an answer carries Telegram inline buttons. A button has NO authority of its own: its callback data is
just the command text ("yes g-abc123"), and the listener runs it through exactly the same path as if the owner had typed it (after checking the
press came from the owner's own chat), and only if the command kind is on ALLOWED_KINDS below. Nothing dangerous (authorize, forge, new
missions, projects, STOP) is ever reachable from a button.

for_reply(kind, text) looks at what was just said and offers the obvious next taps: approve/deny a mission, yes/no on a game fix, keep/discard
a lesson, undo a fix, plus a small menu on the overview screens.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

PREFIX = "do:"
ALLOWED_KINDS = frozenset({
    "fix_yes", "fix_no", "fix_undo", "approve_lesson", "deny_lesson", "retire_lesson", "approve_mission", "deny_mission",
    "approve_standing", "deny_standing", "approve_skill", "deny_skill", "run_all", "dashboard", "games", "fixes", "games_refresh",
    "lessons", "evals", "teach", "fastlane", "teacher", "timeline", "menu", "trades", "level", "doctor", "disk", "homelab", "help", "status",
    "history", "todos", "projects", "ack", "trust", "repo", "repos", "repo_yes", "repo_no", "mem_yes", "mem_no", "memory", "recall", "unforget", "upgrade_yes", "upgrade_no", "upgrade_undo", "upgrade_diff", "upgrade_now", "upgrade_openhands", "upgrades", "money", "money_show", "money_set", "unwrap", "night_now", "transcripts", "earnings", "freelance_status", "freelance_find", "fab_status", "printer", "pihole", "tv", "kuma", "gpu", "content_status", "learn_status", "subscriptions_status", "lab", "gp_status", "gp_disarm", "shards", "land", "land_show", "land_yes", "land_no", "workers", "integ_adopt", "integ_skip", "integrations", "version",
})

MAIN_MENU: List[List[Tuple[str, str]]] = [
    [("▶ Check everything", "all"), ("📊 Dashboard", "dashboard")],
    [("🎮 My games", "games"), ("🔧 Fixes waiting", "fixes")],
    [("🧪 Self-check", "evals"), ("🎓 Lessons", "lessons")],
    [("🕰️ Growth timeline", "timeline"), ("⚡ Fast lane", "fast lane")],
    [("📈 Paper trading", "trades"), ("🛡️ Trust report", "trust")],
    [("📥 Repos", "repos"), ("🧠 Memory", "memory")],
    [("💰 Money ideas", "money"), ("🧪 Strategy lab", "lab")],
    [("🎁 Overnight gifts", "unwrap"), ("🎙️ Transcripts", "transcripts")],
    [("💵 Earnings", "earnings"), ("🧰 Freelance", "freelance")],
    [("📝 Content", "content"), ("💳 Subscriptions", "subscriptions")],
    [("📚 Library", "learn"), ("🤖 Helpers", "shards")],
    [("🛠️ Upgrades", "upgrades"), ("❓ Help", "help")],
]


def _btn(label: str, cmd: str) -> Dict[str, str]:
    return {"text": label, "callback_data": (PREFIX + cmd)[:64]}


def _rows(pairs: List[List[Tuple[str, str]]]) -> List[List[Dict[str, str]]]:
    return [[_btn(a, b) for a, b in row] for row in pairs]


def menu() -> dict:
    return {"inline_keyboard": _rows(MAIN_MENU)}


def command_from_data(data: str) -> Optional[str]:
    """The command text a button stands for, or None if this is not one of ours."""
    if not (data or "").startswith(PREFIX):
        return None
    cmd = data[len(PREFIX):].strip()
    if not (2 <= len(cmd) <= 60 and re.fullmatch(r"[A-Za-z0-9 _:.\-]+", cmd)):
        return None
    if re.match(r"money \S+ \S+$", cmd) and not cmd.endswith((" explore", " reject", " pause")):
        return None                                       # a tap may explore, pause or drop an idea; starting one stays a typed command
    return cmd


def for_reply(kind: str, text: str) -> Optional[dict]:
    """Inline keyboard for a message, or None. `kind` is the command kind that produced it ('' for proactive messages)."""
    t = text or ""
    rows: List[List[Tuple[str, str]]] = []

    m = re.search(r"approve mission (m-[0-9a-f]{6})", t)
    if m:
        rows.append([("✅ Approve", f"approve mission {m.group(1)}"), ("❌ Deny", f"deny mission {m.group(1)}")])
    m = re.search(r"approve standing (sm-[0-9a-f]{6})", t)
    if m:
        rows.append([("✅ Approve", f"approve standing {m.group(1)}"), ("❌ Deny", f"deny standing {m.group(1)}")])
    m = re.search(r"approve skill ([a-z][a-z0-9_]{2,30})", t)
    if m:
        rows.append([("✅ Approve skill", f"approve skill {m.group(1)}"), ("❌ Deny", f"deny skill {m.group(1)}")])

    for uid in list(dict.fromkeys(re.findall(r"\byes (u-[0-9a-f]{6})", t)))[:2]:
        rows.append([("✅ Approve & deploy", f"yes {uid}"), ("❌ Reject", f"no {uid}")])
        rows.append([("🔍 Show the code", f"diff {uid}")])
    for uid in [u for u in dict.fromkeys(re.findall(r"\bundo (u-[0-9a-f]{6})", t)) if f"yes {u}" not in t][:2]:
        rows.append([("↩️ Undo this upgrade", f"undo {uid}")])
    for kid in list(dict.fromkeys(re.findall(r"\byes (k-[0-9a-f]{6})", t)))[:3]:
        if "Forget this memory" in t:
            rows.append([("🗑️ Forget it", f"yes {kid}"), ("✅ Keep it", f"no {kid}")])
        else:
            rows.append([("🧠 Remember", f"yes {kid}"), ("❌ No", f"no {kid}")])
    for rid in list(dict.fromkeys(re.findall(r"\byes (r-[0-9a-f]{6})", t)))[:3]:
        rows.append([("✅ Absorb it", f"yes {rid}"), ("❌ Skip", f"no {rid}")])
    for fid in list(dict.fromkeys(re.findall(r"\byes (f-[0-9a-f]{6})", t)))[:3]:
        rows.append([("✅ Keep it", f"yes {fid}"), ("❌ Discard", f"no {fid}")])
    for cid in list(dict.fromkeys(re.findall(r"\byes (c-[0-9a-f]{6})", t)))[:3]:
        rows.append([("✅ Keep it", f"yes {cid}"), ("❌ Discard", f"no {cid}")])
    for nid in list(dict.fromkeys(re.findall(r"\byes (n-[0-9a-f]{6})", t)))[:3]:
        rows.append([("✅ Keep it", f"yes {nid}"), ("❌ Discard", f"no {nid}")])

    for aid in list(dict.fromkeys(re.findall(r"\byes land (a-[0-9a-f]{6})", t)))[:2]:
        rows.append([("✅ APPROVE", f"yes land {aid}"), ("❌ REJECT", f"no land {aid}")])
    for iid in list(dict.fromkeys(re.findall(r"\badopt (i-[0-9a-f]{6})", t)))[:5]:
        rows.append([(f"✅ Adopt {iid[-6:]}", f"adopt {iid}"), (f"⏭ Skip {iid[-6:]}", f"skip {iid}")])
    for key in list(dict.fromkeys(re.findall(r"\bland show ([A-Za-z0-9_\-]{2,60})", t)))[:3]:
        rows.append([(f"🏞️ {key[:40]}", f"land show {key}")])

    fixes = list(dict.fromkeys(re.findall(r"\byes (g-[0-9a-f]{6})", t)))[:3]
    for gid in fixes:
        rows.append([("✅ Yes, fix it" if len(fixes) == 1 else f"✅ Fix {gid[-6:]}", f"yes {gid}"), ("❌ No" if len(fixes) == 1 else f"❌ Skip {gid[-6:]}", f"no {gid}")])
    for gid in [g for g in dict.fromkeys(re.findall(r"\bundo (g-[0-9a-f]{6})", t)) if g not in fixes][:2]:
        rows.append([("↩️ Undo this fix", f"undo {gid}")])

    lessons = list(dict.fromkeys(re.findall(r"approve lesson (l-[0-9a-f]{6})", t)))[:3]
    for lid in lessons:
        rows.append([("✅ Keep lesson", f"approve lesson {lid}"), ("❌ Discard", f"deny lesson {lid}")])
    if kind == "lessons" and not lessons:
        for lid in list(dict.fromkeys(re.findall(r"📝 <code>(l-[0-9a-f]{6})</code>", t)))[:3]:
            rows.append([(f"✅ Keep {lid[-6:]}", f"approve lesson {lid}"), (f"❌ Discard {lid[-6:]}", f"deny lesson {lid}")])

    for mid in list(dict.fromkeys(re.findall(r"money ([a-z][a-z0-9_]{2,30}) explore", t)))[:2]:
        rows.append([("🔎 Explore it", f"money {mid} explore"), ("🙅 Not for me", f"money {mid} reject")])

    if kind in ("dashboard", "run_all"):
        rows += [[("▶ Check everything", "all"), ("🎮 Games", "games")], [("🔧 Fixes", "fixes"), ("🕰️ Timeline", "timeline")]]
    elif kind == "digest":
        rows.append([("▶ Check everything", "all"), ("📊 Dashboard", "dashboard")])
    elif kind == "unwrap":
        rows.append([("📊 Dashboard", "dashboard"), ("💰 All money ideas", "money")])
    elif kind == "upgrades":
        rows.append([("🛠️ Work on one now", "upgrade now"), ("📊 Dashboard", "dashboard")])
    elif kind == "memory":
        rows.append([("📊 Dashboard", "dashboard")])
    elif kind == "games":
        rows.append([("🔄 Refresh facts", "games refresh"), ("🔧 Fixes", "fixes")])
    elif kind == "fastlane":
        rows.append([("⚡ Turn off", "fast lane off")] if "is ON" in t else [("⚡ Turn on", "fast lane on")])
    elif kind == "evals" and "✗" in t:
        rows.append([("🎓 Learn from failures", "teach"), ("📊 Dashboard", "dashboard")])
    elif kind == "announce":
        rows.append([("👍 OK", "ack"), ("🕰️ Timeline", "timeline")])
    elif kind == "timeline":
        rows.append([("📊 Dashboard", "dashboard"), ("▶ Check everything", "all")])
    elif kind in ("help", "menu"):
        rows = MAIN_MENU + rows
    return {"inline_keyboard": _rows(rows)} if rows else None
