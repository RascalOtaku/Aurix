"""src/foundation/repos.py - send AURIX a repo link, get a plain report, tap yes or no.

  link  ->  request file  ->  host agent (scripts/aurix_absorb_agent.py) clones it READ-ONLY and writes FACTS  ->  analyze() (code, no model)
        ->  a report with a verdict  ->  `yes r-xxxxxx` / `no r-xxxxxx` (or the buttons)  ->  a SIGNED approval file  ->  the host agent moves the
        vetted snapshot into a library folder and writes a result  ->  I tell you.

What "absorb" means here, on purpose: the snapshot is STORED, inert, in a library, pinned to one commit. Nothing from the repo is executed,
installed or imported into AURIX, and none of its text is ever fed to a model or into memory (a README can carry prompt injections). Turning
something in it into a working tool is a separate mission that asks first, like any code from the internet.

The model decides nothing here: parsing, the verdict and the report are plain code. The host agent re-checks the signature, the 24 h expiry and
that the link is a github.com repo. Everything is audited (owner/repo names only, never repo text).
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

from src.foundation import audit, gaming

e = html.escape

APPROVAL_TTL_HOURS = 24
MAX_QUEUED = 3
MAX_PER_DAY = 20
STUCK_MINUTES = 20
RID = re.compile(r"r-[0-9a-f]{6}")
_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_FIND = re.compile(r"github\.com/([A-Za-z0-9-]{1,39})/([A-Za-z0-9_.-]{1,100})", re.I)
_BINARY_EXT = {".exe", ".dll", ".so", ".dylib", ".bin", ".msi", ".apk", ".jar", ".dmg", ".pyd", ".o", ".a", ".class", ".whl"}
_HARD = {"pipe_to_shell": "downloads something and pipes it straight into a shell", "obfuscated_exec": "runs code that is hidden inside an encoded blob",
         "key_files_and_network": "reads key/credential files AND talks to the network"}


def absorb_dir() -> Path:
    return Path(os.environ.get("AURIX_ABSORB_DIR") or (Path(os.environ.get("AURIX_BRAIN", "/aurix")) / "absorb"))


def _data() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data" / "repos"


def _atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def find_repo(text: str) -> Optional[Tuple[str, str]]:
    """(owner, repo) from a message that contains a github.com repo link, else None. Strict: nothing but these two names is ever used."""
    m = _FIND.search(text or "")
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    repo = repo.rstrip(".")
    if not (_OWNER.match(owner) and _REPO.match(repo)) or repo in (".", "..") or ".." in repo:
        return None
    return owner, repo


# ---------------------------------------------------------------------------------------------------------------------------------
# records (one small JSON per link)
# ---------------------------------------------------------------------------------------------------------------------------------

def load(rid: str) -> Optional[dict]:
    if not RID.fullmatch(rid or ""):
        return None
    try:
        return json.loads((_data() / f"{rid}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save(rec: dict) -> None:
    _atomic(_data() / f"{rec['id']}.json", rec)


def all_records() -> List[dict]:
    out = []
    d = _data()
    for f in d.glob("r-*.json") if d.exists() else []:
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return sorted(out, key=lambda r: r.get("created", 0))


def pending() -> List[dict]:
    return [r for r in all_records() if r.get("status") == "review"]


# ---------------------------------------------------------------------------------------------------------------------------------
# asking
# ---------------------------------------------------------------------------------------------------------------------------------

def request(text: str, now: Optional[float] = None) -> str:
    now = now or time.time()
    found = find_repo(text)
    if not found:
        return "I need a GitHub link like <code>https://github.com/owner/repo</code>. Send it and I will look inside (nothing runs)."
    owner, repo = found
    recs = all_records()
    same = [r for r in recs if r["owner"].lower() == owner.lower() and r["repo"].lower() == repo.lower() and r.get("status") in ("queued", "review", "approved")]
    if same:
        r = same[-1]
        return f"I already have <b>{e(owner)}/{e(repo)}</b> ({e(r['status'])}, <code>{r['id']}</code>)." + (" Send <code>repos</code> to see it." if r["status"] != "review" else "\n\n" + render_report(r))
    if len([r for r in recs if r.get("status") == "queued"]) >= MAX_QUEUED:
        return f"I am already fetching {MAX_QUEUED} repos. Give those a minute, then send this one again."
    if len([r for r in recs if now - r.get("created", 0) < 86400]) >= MAX_PER_DAY:
        return f"That is {MAX_PER_DAY} links today, my limit for one day. Try again tomorrow."
    rid = "r-" + secrets.token_hex(3)
    rec = {"id": rid, "owner": owner, "repo": repo, "status": "queued", "created": now}
    save(rec)
    _atomic(absorb_dir() / "requests" / f"{rid}.json", {"id": rid, "owner": owner, "repo": repo, "created": now})
    audit.append("repo_requested", id=rid, repo=f"{owner}/{repo}")
    return (f"📥 Got it: <b>{e(owner)}/{e(repo)}</b> <code>{rid}</code>.\nI am fetching a read-only copy to the server and going through it (nothing gets run or installed). "
            "You will get a report here in a minute or two with a yes/no.")


# ---------------------------------------------------------------------------------------------------------------------------------
# judging the facts (plain code)
# ---------------------------------------------------------------------------------------------------------------------------------

def analyze(facts: dict) -> Dict[str, Any]:
    """{level: ok|careful|stop, reasons: [...], kind: docs|tool|app|empty, what: str} from the host agent's facts. No model involved."""
    reasons: List[str] = []
    level = "ok"
    flags = facts.get("hard_flags") or {}
    for k, why in _HARD.items():
        if flags.get(k):
            level = "stop"
            reasons.append(f"⛔ {why} ({len(flags[k])} place{'s' if len(flags[k]) != 1 else ''})")
    sus = facts.get("suspicious") or []
    if sus:
        level = "stop" if level == "stop" else "careful"
        reasons.append(f"{len(sus)} spot{'s' if len(sus) != 1 else ''} worth a look (shell calls, eval, outbound requests...)")
    hooks = facts.get("install_hooks") or []
    if hooks:
        level = "stop" if level == "stop" else "careful"
        reasons.append("runs code when installed: " + ", ".join(hooks[:4]))
    bins = facts.get("binaries") or []
    if bins:
        level = "stop" if level == "stop" else "careful"
        reasons.append(f"ships {len(bins)} compiled file{'s' if len(bins) != 1 else ''} I cannot read ({', '.join(b['name'] for b in bins[:3])})")
    if not facts.get("license"):
        reasons.append("no license file, so you may not have the right to reuse it")
        level = "stop" if level == "stop" else "careful"
    if facts.get("size_mb", 0) > 150:
        reasons.append(f"large: {facts['size_mb']} MB")
    files, exts = int(facts.get("files", 0)), facts.get("extensions") or {}
    docs = sum(exts.get(x, 0) for x in (".md", ".txt", ".rst", ".mdx"))
    code = sum(exts.get(x, 0) for x in (".py", ".js", ".ts", ".go", ".rs", ".java", ".c", ".cpp", ".cs", ".sh", ".rb", ".php", ".lua"))
    if not files:
        kind = "empty"
    elif code == 0 or docs > 3 * max(code, 1):
        kind = "docs"
    elif files > 400 or code > 250:
        kind = "app"
    else:
        kind = "tool"
    what = {"docs": "mostly text (instructions, prompts or notes), little or no code", "tool": "a small tool or library", "app": "a large application or framework",
            "empty": "nothing usable"}[kind]
    return {"level": level, "reasons": reasons, "kind": kind, "what": what}


def _blurb(readme: str) -> str:
    for para in re.split(r"\n\s*\n", readme or ""):
        line = " ".join(para.split())
        if len(line) >= 30 and not line.startswith(("#", "!", "[", "<", "|", "```", "---", "===")):
            return line[:280]
    return ""


def render_report(rec: dict) -> str:
    f, v = rec.get("facts") or {}, rec.get("verdict") or {}
    icon = {"ok": "🟢", "careful": "🟠", "stop": "🔴"}.get(v.get("level"), "•")
    lines = [f"📥 {icon} <b>{e(rec['owner'])}/{e(rec['repo'])}</b>  <code>{rec['id']}</code>", f"<b>What it is:</b> {e(v.get('what', '?'))}"]
    blurb = _blurb(f.get("readme", ""))
    if blurb:
        lines.append(f"<i>{e(blurb)}</i>")
    lic = f.get("license") or "none found"
    lines.append(f"<b>Size:</b> {f.get('files', 0)} files, {f.get('size_mb', 0)} MB · <b>License:</b> {e(str(lic))} · <b>Last commit:</b> {e(str(f.get('commit_date', ''))[:10])}")
    top = sorted((f.get("extensions") or {}).items(), key=lambda t: -t[1])[:4]
    if top:
        lines.append("<b>Mostly:</b> " + ", ".join(f"{e(k or 'no ext')} ({n})" for k, n in top))
    deps = f.get("dependencies") or []
    if deps:
        lines.append(f"<b>Needs:</b> {e(', '.join(deps[:8]))}" + (f" (+{len(deps) - 8} more)" if len(deps) > 8 else ""))
    if v.get("reasons"):
        lines.append("<b>Things to know:</b>")
        lines += [f"• {e(x)}" if not x.startswith("⛔") else f"• {e(x)}" for x in v["reasons"][:6]]
    for s in (f.get("suspicious") or [])[:3]:
        lines.append(f"   <code>{e(str(s.get('file', ''))[:60])}:{s.get('line')}</code> {e(str(s.get('what', '')))}")
    verdict = {"ok": "Nothing alarming turned up (that is pattern-matching, not a security audit).",
               "careful": "Nothing here is disqualifying, but read the notes above first.",
               "stop": "I would NOT use this as is. You can still store it inert in the library for reading."}[v.get("level", "careful")]
    lines.append(f"<b>My read:</b> {verdict}")
    lines.append("<b>If you say yes:</b> I store this exact commit in the library, untouched and inert. Nothing runs or installs; using any of it is a separate step that asks first.")
    lines.append(f"<b>Your call:</b>  <code>yes {rec['id']}</code>   or   <code>no {rec['id']}</code>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------------------------------------
# deciding
# ---------------------------------------------------------------------------------------------------------------------------------

def _signed(rec: dict, kind: str, now: float) -> dict:
    key = gaming.load_key()
    if key is None:
        raise RuntimeError("AURIX_GAMING_HMAC_KEY is not set on the server")
    from datetime import datetime, timedelta, timezone
    item = {"id": rec["id"], "kind": kind, "owner": rec["owner"], "repo": rec["repo"], "commit": (rec.get("facts") or {}).get("commit", ""),
            "created": datetime.fromtimestamp(now, timezone.utc).astimezone().isoformat(timespec="seconds"),
            "expires": (datetime.fromtimestamp(now, timezone.utc) + timedelta(hours=APPROVAL_TTL_HOURS)).astimezone().isoformat(timespec="seconds")}
    item["sig"] = gaming.sign(key, item)
    _atomic(absorb_dir() / "approved" / f"{rec['id']}.json", item)
    return item


def approve(rid: str, now: Optional[float] = None) -> str:
    rec = load(rid)
    if rec is None:
        return f"No repo {e(rid)}."
    if rec["status"] != "review":
        return f"Repo {rid} is {rec['status']}; nothing to approve."
    now = now or time.time()
    try:
        _signed(rec, "absorb", now)
    except RuntimeError as ex:
        return f"I cannot sign an approval: {e(str(ex))}."
    rec["status"], rec["decided"] = "approved", now
    save(rec)
    audit.append("repo_approved", id=rid, repo=f"{rec['owner']}/{rec['repo']}", commit=(rec.get("facts") or {}).get("commit", "")[:12])
    return f"✅ Approved <code>{rid}</code>. Storing it in the library now; I will tell you when it is done."


def decline(rid: str, now: Optional[float] = None) -> str:
    rec = load(rid)
    if rec is None:
        return f"No repo {e(rid)}."
    if rec["status"] not in ("review", "queued"):
        return f"Repo {rid} is {rec['status']}."
    now = now or time.time()
    try:
        _signed(rec, "discard", now)                                  # tells the host to delete its working copy
    except RuntimeError:
        pass
    rec["status"], rec["decided"] = "declined", now
    save(rec)
    audit.append("repo_declined", id=rid, repo=f"{rec['owner']}/{rec['repo']}")
    return f"Skipped <code>{rid}</code> ({e(rec['owner'])}/{e(rec['repo'])}). I deleted my working copy."


# ---------------------------------------------------------------------------------------------------------------------------------
# reading what the host agent wrote
# ---------------------------------------------------------------------------------------------------------------------------------

def _read(path: Path) -> Optional[dict]:
    try:
        v = json.loads(path.read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else None
    except (OSError, ValueError):
        return None


def tick(now: Optional[float] = None) -> List[str]:
    """Owner messages: new reports, the outcome of approvals, and a nudge if the host agent has not picked something up. Cheap when idle."""
    now = now or time.time()
    msgs: List[str] = []
    ad = absorb_dir()
    for rec in all_records():
        rid, st = rec["id"], rec.get("status")
        if st == "queued":
            facts = _read(ad / "facts" / f"{rid}.json")
            if facts is None:
                if now - rec.get("created", now) > STUCK_MINUTES * 60 and not rec.get("nudged"):
                    rec["nudged"] = True
                    save(rec)
                    msgs.append(f"⚠️ <code>{rid}</code> ({e(rec['owner'])}/{e(rec['repo'])}) is still waiting: the server-side fetcher has not picked it up in {STUCK_MINUTES} min. "
                                "Send the link again in a bit; if it keeps happening the fetcher's schedule needs a look.")
                continue
            if facts.get("error"):
                rec["status"], rec["error"] = "failed", str(facts["error"])[:300]
                save(rec)
                audit.append("repo_failed", id=rid, repo=f"{rec['owner']}/{rec['repo']}")
                msgs.append(f"⚠️ I could not fetch <b>{e(rec['owner'])}/{e(rec['repo'])}</b>: {e(rec['error'])}")
                continue
            rec["facts"], rec["verdict"] = facts, analyze(facts)
            rec["status"], rec["reviewed"] = "review", now
            rec["facts"]["readme"] = str(rec["facts"].get("readme", ""))[:1500]
            save(rec)
            audit.append("repo_reviewed", id=rid, repo=f"{rec['owner']}/{rec['repo']}", level=rec["verdict"]["level"])
            msgs.append(render_report(rec))
        elif st == "approved":
            res = _read(ad / "results" / f"{rid}.json")
            if res is None:
                continue
            if res.get("status") == "applied":
                rec["status"], rec["result"] = "absorbed", res
                audit.append("repo_absorbed", id=rid, repo=f"{rec['owner']}/{rec['repo']}")
                msgs.append(f"📦 <b>Absorbed</b> {e(rec['owner'])}/{e(rec['repo'])} <code>{rid}</code>\n<i>{e(str(res.get('detail', ''))[:300])}</i>\n"
                            "It is stored inert, pinned to that commit. To actually use something from it, tell me what you want (that becomes a mission that asks first).")
            else:
                rec["status"], rec["result"] = "failed", res
                msgs.append(f"⚠️ Could not store <b>{e(rec['owner'])}/{e(rec['repo'])}</b>: {e(str(res.get('status')))} - {e(str(res.get('detail', ''))[:300])}")
            save(rec)
    return msgs


def list_text(limit: int = 8) -> str:
    recs = all_records()[-limit:][::-1]
    if not recs:
        return "No repos yet. Send me a GitHub link (or <code>repo https://github.com/owner/repo</code>) and I will look inside it."
    icon = {"queued": "⏳", "review": "🟠", "approved": "⏳", "absorbed": "📦", "declined": "🚫", "failed": "⚠️"}
    lines = ["<b>Repos</b>"]
    for r in recs:
        extra = ""
        if r["status"] == "review":
            extra = f" · <code>yes {r['id']}</code> / <code>no {r['id']}</code>"
        lines.append(f"{icon.get(r['status'], '•')} <code>{r['id']}</code> {e(r['owner'])}/{e(r['repo'])} · {e(r['status'])}{extra}")
    return "\n".join(lines)


def panel() -> List[Dict[str, Any]]:
    """Dashboard rows: newest first, plain data (the page draws it with textContent)."""
    out = []
    for r in all_records()[-8:][::-1]:
        v = r.get("verdict") or {}
        out.append({"id": r["id"], "name": f"{r['owner']}/{r['repo']}", "status": r["status"], "level": v.get("level", ""), "what": v.get("what", ""),
                    "reasons": (v.get("reasons") or [])[:3]})
    return out
