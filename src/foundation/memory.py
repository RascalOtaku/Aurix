"""src/foundation/memory.py - Foundation 2: one memory you can see, correct and trust.

AURIX already had a real memory (Odysseus's memory.json: pinned facts + BM25/vector recall in every chat, and the /memory editor). It was nearly empty
(one stale, wrong entry), nothing fed it from Telegram, and a static dump of the newest brain notes (mostly auto-written trading summaries) filled the
prompt instead. This module makes that one store the single source of truth and adds what was missing:

  * provenance   every entry AURIX writes carries where it came from (owner:telegram, owner:message, ...) and why
  * intake       `remember: ...` from Telegram; and code-only detection of things you say about yourself ("I prefer...", "my X is..."), which become
                 PROPOSALS you approve with one tap - a model never writes to memory on its own, and nothing sensitive is ever proposed
  * forgetting   `forget ...` (undoable), and a daily pass that PROPOSES forgetting entries that look stale or are prompt-dumps
  * seeing it    search, list and a dashboard card with pin / forget buttons

Everything reads and writes through MemoryManager so the existing chat recall, pins and editor keep working unchanged. Audit records carry ids and
sources only, never the text of a memory.
"""
from __future__ import annotations

import html
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.foundation import audit

e = html.escape

KID = re.compile(r"k-[0-9a-f]{6}")
MAX_PENDING = 8
MAX_NEW_PER_DAY = 6
STALE_DAYS = 120
PROMPT_DUMP_CHARS = 500
FORGOTTEN_KEEP = 50
_STOP = set("the a an and or but if then of to in on for with at by from is are was were be been am i me my you your it its this that these those do does did not no yes so as".split())

# Never proposed, never stored by the automatic intake (an explicit `remember:` from you is still your call, but these are refused too:
# a memory is injected into prompts, and a prompt is not a safe place for a secret).
SENSITIVE = re.compile(r"(?i)\b(password|passwd|passphrase|api[-_ ]?key|secret|token|private key|ssn|social security|credit card|card number|cvv|pin code|seed phrase|2fa|otp)\b|\b\d{3}-\d{2}-\d{4}\b|\b(?:\d[ -]?){13,19}\b|sk-[A-Za-z0-9]{16,}")

_PATTERNS = [
    (re.compile(r"\bI\s+(?:really\s+|strongly\s+|generally\s+)?(?:prefer|like|love|enjoy|hate|dislike|can't stand|want|need|usually|always|never)\b[^.!?\n]{4,160}", re.I), "preference"),
    (re.compile(r"\bmy\s+(?:favou?rite|goal|plan|dog|cat|wife|husband|partner|girlfriend|boyfriend|kid|son|daughter|brother|sister|mom|dad|birthday|job|car|truck|pc|computer|phone|address|name|email|schedule|budget)\b[^.!?\n]{0,60}?\b(?:is|are|was|will be)\b[^.!?\n]{2,120}", re.I), "fact"),
    (re.compile(r"\b(?:from now on|going forward|in future|don'?t ever|never ever|always)\b[^.!?\n]{5,160}", re.I), "rule"),
    (re.compile(r"\b(?:remember|note|keep in mind|fyi)\s+that\s+[^.!?\n]{5,200}", re.I), "fact"),
    (re.compile(r"\bI(?:'m| am)\s+(?:allergic|vegan|vegetarian|left-handed|right-handed|color-?blind|a\s+[a-z ]{3,30}|working on|building|learning)\b[^.!?\n]{0,120}", re.I), "fact"),
]


# ---------------------------------------------------------------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------------------------------------------------------------

def _root() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app"))


def _data() -> Path:
    return _root() / "data"


def _mem_dir() -> Path:
    return _data() / "memory"


def _owner() -> str:
    return os.getenv("AURIX_MEMORY_OWNER", "admin")


def _mm():
    from src.memory import MemoryManager
    return MemoryManager(str(_data()))


def _atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def entries() -> List[dict]:
    """The owner's memories, newest first."""
    mine = [m for m in _mm().load_all() if m.get("owner") in (_owner(), None, "")]
    return sorted(mine, key=lambda m: m.get("timestamp", 0), reverse=True)


def short(mid: str) -> str:
    return str(mid)[:8]


def _tokens(text: str) -> List[str]:
    return [w for w in re.findall(r"[a-z0-9][a-z0-9'-]{2,}", (text or "").lower()) if w not in _STOP]


def _similar(a: str, b: str) -> float:
    ta, tb = set(_tokens(a)), set(_tokens(b))
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def _category(text: str) -> str:
    t = text.lower()
    if re.search(r"\b(prefer|like|love|hate|dislike|favou?rite|always|never|usually)\b", t):
        return "preference"
    if re.search(r"\b(wife|husband|partner|girlfriend|boyfriend|kid|son|daughter|brother|sister|mom|dad|friend|dog|cat)\b", t):
        return "person"
    if re.search(r"\b(project|building|working on|goal|plan)\b", t):
        return "project"
    return "fact"


# ---------------------------------------------------------------------------------------------------------------------------------
# writing and forgetting
# ---------------------------------------------------------------------------------------------------------------------------------

def remember(text: str, source: str = "owner:telegram", why: str = "", pinned: bool = False, confidence: float = 1.0) -> str:
    text = " ".join((text or "").split())[:400]
    if len(text) < 4:
        return "Tell me what to remember, e.g. <code>remember: I prefer short answers</code>."
    if SENSITIVE.search(text):
        return "🔒 That looks like a secret (password, key, card or ID number). I do not put secrets in memory, because memory goes into prompts. Keep it in your password manager."
    mm = _mm()
    mine = entries()
    for m in mine:
        if m["text"].strip().lower() == text.lower() or _similar(m["text"], text) >= 0.85:
            return f"I already remember that: “{e(m['text'][:120])}” (<code>{short(m['id'])}</code>)."
    entry = mm.add_entry(text, source="user", category=_category(text), owner=_owner())
    entry.update({"provenance": source, "why": why[:200], "confidence": confidence, "pinned": bool(pinned)})
    allm = mm.load_all()
    allm.append(entry)
    mm.save(allm)
    audit.append("memory_added", id=short(entry["id"]), source=source, category=entry["category"], pinned=bool(pinned))
    return f"🧠 Remembered <code>{short(entry['id'])}</code>: “{e(text[:160])}”" + (" (pinned: always in context)" if pinned else "") + f"\nChange your mind with <code>forget {short(entry['id'])}</code>."


def _find(ref: str) -> Tuple[Optional[dict], str]:
    ref = (ref or "").strip().strip("`<>").lower()
    if not ref:
        return None, "Tell me which one: an id like <code>forget 3fa9c210</code>, or some of its words."
    mine = entries()
    if re.fullmatch(r"[0-9a-f]{6,36}(?:-[0-9a-f-]*)?", ref):
        hits = [m for m in mine if str(m["id"]).lower().startswith(ref)]
        if len(hits) == 1:
            return hits[0], ""
        if len(hits) > 1:
            return None, "That id matches more than one memory; give a few more characters."
    scored = sorted(((_similar(ref, m["text"]) + (0.5 if ref in m["text"].lower() else 0.0), m) for m in mine), key=lambda t: -t[0])
    good = [m for s, m in scored if s >= 0.34]
    if len(good) == 1 or (good and scored[0][0] - scored[1][0] >= 0.25):
        return good[0], ""
    if good:
        return None, "More than one memory fits; use an id:\n" + "\n".join(f"• <code>{short(m['id'])}</code> {e(m['text'][:80])}" for m in good[:5])
    return None, f"I do not have a memory like “{e(ref[:60])}”. <code>memory</code> lists what I have."


def forget(ref: str) -> str:
    m, err = _find(ref)
    if m is None:
        return err
    mm = _mm()
    rest = [x for x in mm.load_all() if x.get("id") != m["id"]]
    mm.save(rest)
    tomb = _mem_dir() / "forgotten.json"
    try:
        old = json.loads(tomb.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = []
    old.append({"entry": m, "at": time.time()})
    _atomic(tomb, old[-FORGOTTEN_KEEP:])
    audit.append("memory_forgotten", id=short(m["id"]))
    return f"🗑️ Forgot <code>{short(m['id'])}</code>: “{e(m['text'][:120])}”. Changed your mind? Send <code>unforget</code>."


def unforget() -> str:
    tomb = _mem_dir() / "forgotten.json"
    try:
        old = json.loads(tomb.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = []
    if not old:
        return "Nothing to bring back."
    last = old.pop()
    mm = _mm()
    allm = mm.load_all()
    if not any(x.get("id") == last["entry"].get("id") for x in allm):
        allm.append(last["entry"])
        mm.save(allm)
    _atomic(tomb, old)
    audit.append("memory_restored", id=short(last["entry"].get("id", "")))
    return f"↩️ Brought back <code>{short(last['entry'].get('id', ''))}</code>: “{e(str(last['entry'].get('text', ''))[:120])}”."


def pin(ref: str, on: bool = True) -> str:
    m, err = _find(ref)
    if m is None:
        return err
    mm = _mm()
    allm = mm.load_all()
    for x in allm:
        if x.get("id") == m["id"]:
            x["pinned"] = bool(on)
    mm.save(allm)
    audit.append("memory_pinned", id=short(m["id"]), pinned=bool(on))
    return ("📌 Pinned" if on else "Unpinned") + f" <code>{short(m['id'])}</code>: " + ("it is now always in context." if on else "it is recalled only when relevant.")


# ---------------------------------------------------------------------------------------------------------------------------------
# seeing it
# ---------------------------------------------------------------------------------------------------------------------------------

def search(query: str, k: int = 6) -> List[dict]:
    q = set(_tokens(query))
    if not q:
        return []
    out = []
    for m in entries():
        t = set(_tokens(m["text"]))
        hit = len(q & t)
        if hit:
            out.append((hit / len(q) + min(int(m.get("uses", 0) or 0), 5) * 0.02 + (0.1 if m.get("pinned") else 0.0), m))
    return [m for _, m in sorted(out, key=lambda t: -t[0])[:k]]


def _age_days(m: dict, now: Optional[float] = None) -> int:
    return int(((now or time.time()) - float(m.get("timestamp", 0) or 0)) / 86400)


def _line(m: dict) -> str:
    return f"{'📌' if m.get('pinned') else '•'} <code>{short(m['id'])}</code> {e(m['text'][:140])} <i>({e(str(m.get('provenance') or m.get('source', 'unknown')))}, {_age_days(m)}d, used {int(m.get('uses', 0) or 0)}x)</i>"


def list_text(limit: int = 10) -> str:
    mine = entries()
    pend = pending()
    if not mine:
        return ("I do not have any memories yet. Tell me things: <code>remember: I prefer short answers</code>. "
                "When you say something about yourself (\"I prefer...\", \"my X is...\") I will offer to remember it with one tap.")
    pinned = [m for m in mine if m.get("pinned")]
    lines = [f"<b>Memory</b> · {len(mine)} remembered, {len(pinned)} pinned" + (f", {len(pend)} waiting for your yes/no" if pend else "")]
    lines += [_line(m) for m in (pinned + [m for m in mine if not m.get("pinned")])[:limit]]
    if len(mine) > limit:
        lines.append(f"<i>...and {len(mine) - limit} more. <code>what do you remember about &lt;topic&gt;</code> searches.</i>")
    lines.append("<code>remember: ...</code> · <code>forget &lt;id or words&gt;</code> · <code>unforget</code>")
    return "\n".join(lines)


def recall_text(query: str) -> str:
    hits = search(query)
    if not hits:
        return f"I do not remember anything about “{e(query[:60])}”. Tell me with <code>remember: ...</code>."
    return f"<b>What I remember about “{e(query[:60])}”</b>\n" + "\n".join(_line(m) for m in hits)


# ---------------------------------------------------------------------------------------------------------------------------------
# proposals: things worth remembering (from what you say) and things worth forgetting (stale entries) - you decide with yes / no
# ---------------------------------------------------------------------------------------------------------------------------------

def _pdir() -> Path:
    return _mem_dir() / "proposals"


def load_proposal(pid: str) -> Optional[dict]:
    if not KID.fullmatch(pid or ""):
        return None
    try:
        return json.loads((_pdir() / f"{pid}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_proposal(p: dict) -> None:
    _atomic(_pdir() / f"{p['id']}.json", p)


def all_proposals() -> List[dict]:
    out = []
    for f in _pdir().glob("k-*.json") if _pdir().exists() else []:
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return sorted(out, key=lambda p: p.get("created", 0))


def pending() -> List[dict]:
    return [p for p in all_proposals() if p.get("status") == "pending"]


def candidates(text: str) -> List[Tuple[str, str]]:
    """(sentence, category) durable things you said about yourself. Plain patterns, no model; long pastes, questions, code and secrets are skipped."""
    t = (text or "").strip()
    if not t or len(t) > 400 or "```" in t or "http" in t.lower() or SENSITIVE.search(t):
        return []
    out: List[Tuple[str, str]] = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", t):
        s = sentence.strip()
        if len(s) < 12 or s.endswith("?"):
            continue
        for rx, cat in _PATTERNS:
            m = rx.search(s)
            if m:
                out.append((s[:220], cat))
                break
    return out[:3]


def observe(text: str, now: Optional[float] = None) -> int:
    """Called for every message you send that is not a command. Files proposals (never memories); returns how many. Cheap and silent."""
    from src.foundation import shards
    if shards.is_paused("memory"):
        return 0
    now = now or time.time()
    made = 0
    props = all_proposals()
    if len([p for p in props if now - p.get("created", 0) < 86400]) >= MAX_NEW_PER_DAY or len([p for p in props if p.get("status") == "pending"]) >= MAX_PENDING:
        return 0
    have = [m["text"] for m in entries()] + [p.get("text", "") for p in props if p.get("status") in ("pending", "kept")]
    for sentence, cat in candidates(text):
        if any(_similar(sentence, h) >= 0.7 for h in have):
            continue
        p = {"id": "k-" + secrets.token_hex(3), "kind": "add", "text": sentence, "category": cat, "why": "you said this", "status": "pending", "created": now}
        save_proposal(p)
        audit.append("memory_proposed", id=p["id"], kind="add", category=cat)
        have.append(sentence)
        made += 1
    return made


def audit_store(now: Optional[float] = None) -> int:
    """Once a day: PROPOSE forgetting entries that look stale (never used for months) or like a pasted prompt. Pinned entries are left alone."""
    now = now or time.time()
    stamp = _mem_dir() / "audit_state.json"
    try:
        if now - float(json.loads(stamp.read_text(encoding="utf-8")).get("at", 0)) < 86400:
            return 0
    except (OSError, ValueError):
        pass
    _atomic(stamp, {"at": now})
    known = {p.get("target") for p in all_proposals() if p.get("kind") == "forget"}
    made = 0
    for m in entries():
        if m.get("pinned") or m["id"] in known:
            continue
        reason = ""
        if len(m["text"]) > PROMPT_DUMP_CHARS or re.match(r"(?i)\s*you are\b", m["text"]):
            reason = "it is a long block that reads like a pasted prompt, not a fact"
        elif _age_days(m, now) >= STALE_DAYS and int(m.get("uses", 0) or 0) == 0:
            reason = f"it is {_age_days(m, now)} days old and has never been used in a chat"
        if not reason:
            continue
        p = {"id": "k-" + secrets.token_hex(3), "kind": "forget", "target": m["id"], "text": m["text"][:300], "why": reason, "status": "pending", "created": now}
        save_proposal(p)
        audit.append("memory_proposed", id=p["id"], kind="forget", target=short(m["id"]))
        made += 1
        if made >= 3:
            break
    return made


def render_proposal(p: dict) -> str:
    if p.get("kind") == "forget":
        return (f"🗑️ <b>Forget this memory?</b>  <code>{p['id']}</code>\n“{e(p.get('text', '')[:200])}”\n<i>Why I ask: {e(p.get('why', ''))}.</i>\n"
                f"<code>yes {p['id']}</code> forgets it (undo with <code>unforget</code>) · <code>no {p['id']}</code> keeps it")
    return (f"🧠 <b>Remember this?</b>  <code>{p['id']}</code>\n“{e(p.get('text', '')[:200])}”\n<i>{e(p.get('why', ''))}</i>\n"
            f"<code>yes {p['id']}</code> remembers it · <code>no {p['id']}</code> drops it")


def approve(pid: str) -> str:
    p = load_proposal(pid)
    if p is None:
        return f"No memory note {e(pid)}."
    if p["status"] != "pending":
        return f"{pid} is {p['status']}; nothing to do."
    p["status"], p["decided"] = "kept" if p["kind"] == "add" else "forgotten", time.time()
    save_proposal(p)
    audit.append("memory_proposal_yes", id=pid, kind=p["kind"])
    if p["kind"] == "add":
        return remember(p["text"], source="owner:message", why=p.get("why", ""), confidence=0.8)
    return forget(short(p["target"]))


def decline(pid: str) -> str:
    p = load_proposal(pid)
    if p is None:
        return f"No memory note {e(pid)}."
    if p["status"] != "pending":
        return f"{pid} is {p['status']}; nothing to do."
    p["status"], p["decided"] = "dropped" if p["kind"] == "add" else "kept", time.time()
    save_proposal(p)
    audit.append("memory_proposal_no", id=pid, kind=p["kind"])
    return "Okay, I will not remember that." if p["kind"] == "add" else "Okay, I will keep it (and not ask again)."


def tick(now: Optional[float] = None) -> List[str]:
    """Owner messages: new proposals (at most 3 at a time), after the daily stale-entry pass. Cheap when idle."""
    now = now or time.time()
    try:
        audit_store(now)
    except Exception:
        pass
    msgs = []
    fresh = [p for p in pending() if not p.get("announced")]
    for p in fresh[:3]:
        msgs.append(render_proposal(p))
        p["announced"] = now
        save_proposal(p)
    if len(fresh) > 3:
        msgs.append(f"<i>{len(fresh) - 3} more memory notes are waiting. Send <code>memory</code> to see them.</i>")
        for p in fresh[3:]:
            p["announced"] = now
            save_proposal(p)
    return msgs


def panel(limit: int = 24) -> Dict[str, Any]:
    """Dashboard data: plain values only (the page draws them with textContent)."""
    mine = entries()
    prov: Dict[str, int] = {}
    for m in mine:
        k = str(m.get("provenance") or m.get("source") or "unknown")
        prov[k] = prov.get(k, 0) + 1
    rows = [{"id": short(m["id"]), "text": m["text"][:200], "category": m.get("category", "fact"), "pinned": bool(m.get("pinned")),
             "source": str(m.get("provenance") or m.get("source") or "unknown"), "uses": int(m.get("uses", 0) or 0), "age_days": _age_days(m)}
            for m in (sorted(mine, key=lambda m: (not m.get("pinned"), -m.get("timestamp", 0))))[:limit]]
    try:
        forgotten = len(json.loads((_mem_dir() / "forgotten.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        forgotten = 0
    return {"total": len(mine), "pinned": sum(1 for m in mine if m.get("pinned")), "sources": prov, "rows": rows, "forgotten": forgotten,
            "pending": [{"id": p["id"], "kind": p["kind"], "text": p.get("text", "")[:200], "why": p.get("why", "")} for p in pending()[:8]]}
