"""src/foundation/build_approvals.py - deliver the build pipeline's approval cards (aurix_approval.py) to the owner on Telegram.

The build brain (aurix_build_brain.py) runs as its own process - on the GPU PC or the 7070 - and, when a safety gate says APPROVAL,
writes the request through aurix_approval.ApprovalManager into its RecoveryState folder:

    $AURIX_BRAIN_STATE/contracts/approval-<id>.json      (default ~/.aurix/brain-state, the brain's own default)

This module is the 7070 side of that hand-off:
  scan()      on the scheduler tick: every PENDING request not delivered yet becomes a one-tap card (text + Approve/Deny buttons).
              The listener sends it as an UNPROMPTED message, so it goes through the outbox: a 2 AM request waits for the 6 AM
              summary, buttons kept, unless the card is marked urgent (🚨), which may wake you.
  resolve()   `yes build <id>` / `no build <id>` (typed or tapped) resolves it through ApprovalManager.resolve(), the same call the
              pipeline polls, so the brain resumes or aborts. Only the owner's own chat can reach it (commands.py is owner-only).

Nothing here decides anything: it renders the card the pipeline built, and records the owner's decision.
"""
from __future__ import annotations

import html
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.foundation import audit

e = html.escape
ID_RX = re.compile(r"^\d{8}-\d{6}-\d{6}$")
MAX_DETAIL_CHARS = 700


def state_dir() -> Path:
    return Path(os.environ.get("AURIX_BRAIN_STATE") or os.path.expanduser("~/.aurix/brain-state"))


def _delivered_path() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data" / "build_approvals" / "delivered.json"


def _delivered() -> Dict[str, float]:
    try:
        v = json.loads(_delivered_path().read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_delivered(d: Dict[str, float]) -> None:
    p = _delivered_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    keep = dict(sorted(d.items(), key=lambda kv: kv[1])[-500:])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(keep), encoding="utf-8")
    os.replace(tmp, p)


def present() -> bool:
    """Is there a build pipeline state folder here at all? (Cheap: checked on every scheduler tick.)"""
    return (state_dir() / "contracts").is_dir()


def pending() -> List[dict]:
    """Every pending approval request the build brain has written (its ApprovalRequest.to_dict()), oldest first."""
    folder = state_dir() / "contracts"
    out = []
    try:
        files = sorted(folder.glob("approval-*.json"))
    except OSError:
        return []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        req = (data or {}).get("approval") or {}
        if isinstance(req, dict) and req.get("status") == "pending" and ID_RX.match(str(req.get("id", ""))):
            out.append(req)
    return out


def _card(req: dict) -> dict:
    """The pipeline's own card: ApprovalRequest.to_card(), so the 7070 shows exactly what the pipeline built."""
    try:
        from aurix_approval import ApprovalRequest
        return ApprovalRequest.from_dict(req).to_card()
    except Exception:                                              # noqa: BLE001 - a malformed request still gets a plain card
        return {"id": req.get("id"), "header": f"Approval needed: {req.get('action', '?')}",
                "body": {"action": req.get("action"), "target": req.get("target"), "why": req.get("reason"),
                         "details": req.get("risk_details") or {}}}


def is_urgent(card: dict) -> bool:
    return "🚨" in str(card.get("header", "")) or bool(card.get("urgent"))


def render(card: dict) -> Tuple[str, dict]:
    """(HTML text, inline keyboard) for one to_card() dict."""
    body = card.get("body") or {}
    rid = str(card.get("id", ""))
    details = body.get("details") or {}
    try:
        detail_text = json.dumps(details, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        detail_text = str(details)
    if len(detail_text) > MAX_DETAIL_CHARS:
        detail_text = detail_text[:MAX_DETAIL_CHARS] + "…"
    header = str(card.get("header") or "Approval needed")
    lines = [("" if header.lstrip().startswith("🚨") else "🛠️ ") + f"<b>{e(header)}</b>",
             f"<b>Target:</b> {e(str(body.get('target', '')))}",
             f"<b>Why it stopped:</b> {e(str(body.get('why', '')))}"]
    if details:
        lines.append(f"<b>Gate details:</b> <code>{e(detail_text)}</code>")
    lines.append(f"<i>Build pipeline request {e(rid)}. Tap to decide, or type</i> <code>yes build {e(rid)}</code> / <code>no build {e(rid)}</code>.")
    kb = {"inline_keyboard": [[{"text": "✅ Approve", "callback_data": f"do:yes build {rid}"},
                               {"text": "❌ Deny", "callback_data": f"do:no build {rid}"}]]}
    return "\n".join(lines), kb


def scan(now: Optional[float] = None) -> List[Tuple[str, dict]]:
    """New pending requests as (text, buttons), each delivered once. Never raises."""
    try:
        delivered = _delivered()
        out = []
        for req in pending():
            if req["id"] in delivered:
                continue
            card = _card(req)
            text, kb = render(card)
            if is_urgent(card) and not text.startswith("🚨"):
                text = "🚨 " + text
            out.append((text, kb))
            delivered[req["id"]] = now or time.time()
            audit.append("build_approval_delivered", id=req["id"])
        if out:
            _save_delivered(delivered)
        return out
    except Exception:                                              # noqa: BLE001 - the scheduler must never break on this
        return []


def resolve(request_id: str, approve: bool) -> str:
    """The owner's decision, through the pipeline's own ApprovalManager.resolve()."""
    rid = (request_id or "").strip()
    if not ID_RX.match(rid):
        return "That is not a build approval id."
    try:
        from aurix_approval import ApprovalManager
        from aurix_build_brain import RecoveryState
        mgr = ApprovalManager(recovery=RecoveryState(state_dir=str(state_dir())))
    except Exception as exc:                                       # noqa: BLE001
        return f"The build pipeline's approval store is not reachable here ({type(exc).__name__}). Nothing was decided."
    req = mgr.check(rid)
    if req is None:
        return f"No build approval request <code>{e(rid)}</code> (it may live on another machine: set AURIX_BRAIN_STATE)."
    if req.status != "pending":
        return f"Build request <code>{e(rid)}</code> was already {e(req.status)}."
    done = mgr.resolve(rid, "approved" if approve else "denied", resolved_by="owner-telegram")
    audit.append("build_approval_resolved", id=rid, decision=done.status)
    verb = "✅ Approved" if approve else "❌ Denied"
    return f"{verb}: <b>{e(req.action)}</b> on {e(req.target)}. The build pipeline picks this up on its next check."
