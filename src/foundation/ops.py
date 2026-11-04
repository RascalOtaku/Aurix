"""src/foundation/ops.py - AURIX looking after its own house: update itself, prune GitHub branches, set its CI secret.

The app runs in a container and decides nothing outside it. Like the upgrade lane, it writes a SIGNED request (HMAC, the same key) into
the shared folder; the host-side agent (scripts/aurix_ops_agent.py, run by the upgrade agent's existing cron every minute) verifies it and
does exactly that one whitelisted operation - nothing else - then writes a result the app reports back.

    update aurix      card -> yes ops o-xxxxxx -> host: aurix_git_sync.sh sync (pull GitHub, merge, push this machine's edits; stops on a
                      conflict or a secret) -> aurix_deploy.sh (rebuild, health check, rolls back by itself) -> AURIX runs `econ check`
    repo cleanup      host scans GitHub's branches (read-only, no card needed) -> a card naming EXACTLY which branches it would delete and
                      why (merged into main, or its tip contains your private patterns) -> yes ops -> host deletes those, re-checking each
    ci secret         card -> yes ops -> host sets the AURIX_PRIVATE_PATTERNS repository secret from its own patterns file with the GitHub
                      CLI; the patterns never pass through the app or the chat
    ops               what is waiting, running or done

Never: `main`, a branch with no reason to go, force-push, or any operation not in OPS. Every change is a card you tap.
"""
from __future__ import annotations

import html
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.foundation import audit, gaming

e = html.escape
OPS = ("update", "scan", "prune", "secret")
NEEDS_OK = ("update", "prune", "secret")
ID_RX = re.compile(r"^o-[0-9a-f]{6}$")
BRANCH_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,120}$")
TTL_HOURS = 24
DESCRIBE = {
    "update": ("Update AURIX from GitHub", "The server pulls GitHub's main, merges this machine's own edits and pushes them (it stops on a "
               "conflict or anything secret-looking), rebuilds, and health-checks. A build that does not come up is rolled back by itself. "
               "Then I run <code>econ check</code> on the new build and tell you."),
    "secret": ("Set the CI private-patterns secret", "The server sets the GitHub secret AURIX_PRIVATE_PATTERNS from its own "
               ".git/info/aurix-private-patterns with the GitHub CLI, so CI checks every push from every machine and agent for your "
               "name, hostnames and IPs. The patterns never pass through me or this chat. Needs <code>gh auth login</code> done once on the server."),
}


def ops_dir() -> Path:
    from src.foundation import upgrades
    return upgrades.upgrades_dir() / "ops"


def _state_path() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data" / "ops" / "requests.json"


def _read(p: Path, default: Any) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _atomic(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def requests() -> List[dict]:
    return _read(_state_path(), [])


def _save(items: List[dict]) -> None:
    _atomic(_state_path(), items[-100:])


def _get(oid: str) -> Optional[dict]:
    return next((r for r in requests() if r["id"] == oid), None)


def _put(req: dict) -> None:
    items = [r for r in requests() if r["id"] != req["id"]] + [req]
    _save(items)


def _sign_and_send(req: dict, now: float) -> None:
    key = gaming.load_key()
    if key is None:
        raise RuntimeError("AURIX_GAMING_HMAC_KEY is not set on the server")
    item = {"id": req["id"], "op": req["op"], "branches": req.get("branches", []),
            "created": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
            "expires": (datetime.fromtimestamp(now, timezone.utc) + timedelta(hours=TTL_HOURS)).isoformat(timespec="seconds")}
    item["sig"] = gaming.sign(key, item)
    _atomic(ops_dir() / "approved" / f"{req['id']}.json", item)


def propose(op: str, branches: Optional[List[str]] = None, now: Optional[float] = None) -> str:
    """A card for an operation that changes something (update, prune, secret); `scan` is read-only and is sent straight away."""
    now = now or time.time()
    if op not in OPS:
        return "Unknown operation."
    if op == "scan":
        req = {"id": "o-" + secrets.token_hex(3), "op": "scan", "status": "sent", "created": now}
        try:
            _sign_and_send(req, now)
        except RuntimeError as ex:
            return f"I cannot sign a request: {e(str(ex))}."
        _put(req)
        audit.append("ops_requested", id=req["id"], op="scan")
        return ("🔎 Checking GitHub's branches (read-only). I will send you the exact list I would delete, as a card, in a minute or two.")
    open_ = next((r for r in requests() if r["op"] == op and r["status"] == "pending"), None)
    if open_ and op != "prune":
        return render(open_)
    req = {"id": "o-" + secrets.token_hex(3), "op": op, "status": "pending", "created": now, "branches": branches or []}
    _put(req)
    audit.append("ops_proposed", id=req["id"], op=op, branches=len(req["branches"]))
    return render(req)


def render(req: dict) -> str:
    oid = req["id"]
    if req["op"] == "prune":
        lines = [f"🧹 <b>Delete {len(req['branches'])} GitHub branch(es)</b> <code>{oid}</code>"]
        lines += [f"• <code>{e(b['name'])}</code> - {e(b['why'])}" for b in req["branches"]]
        lines.append("Deleting a branch removes it from GitHub's branch list; if its commits are already in main nothing is lost. "
                     "It does NOT erase old commits from main's history.")
    else:
        title, body = DESCRIBE[req["op"]]
        lines = [f"🛠️ <b>{title}</b> <code>{oid}</code>", body]
    lines.append(f"<code>yes ops {oid}</code> / <code>no ops {oid}</code>")
    return "\n".join(lines)


def decide(oid: str, approve: bool, now: Optional[float] = None) -> str:
    now = now or time.time()
    req = _get((oid or "").lower())
    if not req:
        return f"No operation <code>{e(oid)}</code>."
    if req["status"] != "pending":
        return f"<code>{e(oid)}</code> is already {req['status']}."
    if now - req["created"] > TTL_HOURS * 3600:
        req["status"] = "expired"
        _put(req)
        return f"<code>{e(oid)}</code> expired; ask again."
    if not approve:
        req["status"] = "declined"
        _put(req)
        audit.append("ops_declined", id=req["id"], op=req["op"])
        return "❌ Declined. Nothing was changed."
    try:
        _sign_and_send(req, now)
    except RuntimeError as ex:
        return f"I cannot sign an approval: {e(str(ex))}."
    req["status"], req["decided"] = "sent", now
    _put(req)
    audit.append("ops_approved", id=req["id"], op=req["op"], branches=[b["name"] for b in req.get("branches", [])])
    return f"✅ Approved <code>{req['id']}</code>. The server picks it up within a minute; I will tell you how it went."


def _prune_card_from_scan(res: dict, now: float) -> str:
    cands = [b for b in (res.get("candidates") or []) if isinstance(b, dict) and BRANCH_RX.match(str(b.get("name", "")))
             and b.get("name") not in ("main", "HEAD")]
    if not cands:
        return "🧹 GitHub's branches are clean: nothing merged-and-forgotten and nothing carrying your private patterns."
    return propose("prune", [{"name": str(b["name"]), "why": str(b.get("why", ""))[:120]} for b in cands[:30]], now)


def awaiting_results() -> bool:
    """Cheap check for the scheduler: is any request out with the host?"""
    return any(r.get("status") == "sent" for r in requests())


def tick(now: Optional[float] = None) -> List[str]:
    """Results from the host, turned into messages (and, after a scan, the prune card). Never raises."""
    now = now or time.time()
    msgs: List[str] = []
    try:
        rd = ops_dir() / "results"
        for req in requests():
            if req["status"] != "sent":
                continue
            res = _read(rd / f"{req['id']}.json", None)
            if not isinstance(res, dict):
                continue
            req["status"], req["result"] = ("done" if res.get("ok") else "failed"), {k: res.get(k) for k in ("ok", "detail")}
            _put(req)
            audit.append("ops_result", id=req["id"], op=req["op"], ok=bool(res.get("ok")))
            detail = e(str(res.get("detail", ""))[:600])
            if req["op"] == "scan":
                msgs.append(_prune_card_from_scan(res, now) if res.get("ok") else f"⚠️ The branch scan did not run: {detail}")
            elif req["op"] == "update":
                if res.get("ok"):
                    msgs.append(f"🚀 <b>AURIX is updated and healthy.</b>\n<i>{detail}</i>\n" + _econ_check())
                else:
                    msgs.append(f"⚠️ <b>Update did not complete:</b> {detail}\nThe running app is the previous, healthy build.")
            elif req["op"] == "prune":
                msgs.append(("🧹 Branches deleted: " if res.get("ok") else "⚠️ Branch cleanup incomplete: ") + detail)
            else:
                msgs.append(("🔐 CI secret set: " if res.get("ok") else "⚠️ CI secret not set: ") + detail)
    except Exception:                                             # noqa: BLE001 - the scheduler must never break on this
        pass
    return msgs


def _econ_check() -> str:
    try:
        from src.foundation.economy import check
        return check.run()
    except Exception as ex:                                       # noqa: BLE001
        return f"(econ check could not run: {type(ex).__name__})"


def status_text() -> str:
    items = requests()[-8:][::-1]
    if not items:
        return ("🛠️ <b>Housekeeping</b>: nothing yet. <code>update aurix</code> · <code>repo cleanup</code> · <code>ci secret</code>")
    lines = ["🛠️ <b>Housekeeping</b>"]
    for r in items:
        when = datetime.fromtimestamp(r["created"]).strftime("%m-%d %H:%M")
        lines.append(f"<code>{r['id']}</code> {r['op']} · {r['status']} · {when}"
                     + (f" - {e(str((r.get('result') or {}).get('detail', ''))[:120])}" if r.get("result") else ""))
    lines.append("<code>update aurix</code> · <code>repo cleanup</code> · <code>ci secret</code>")
    return "\n".join(lines)
