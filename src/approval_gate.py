"""src/approval_gate.py - owner approval for consequential agent tool calls.

When the agent wants to run a consequential tool (shell, file writes, outbound
email, settings/token changes, ...) in a gated session, the call is HELD: the
owner is sent the exact action on Telegram, and it only executes if the owner
replies `approve <id>`. `deny <id>`, silence past the timeout, a failed
notification, or any internal error all mean the call does NOT run (fail closed).

Scope
-----
APPROVAL_GATE_MODE (env):
  telegram  (default) gate the Telegram agent session (TELEGRAM_AGENT_SESSION_ID)
  all       gate every session - strictest, pages the owner for each web-UI bash
  off       disabled
Any unrecognised value is treated as `all`, so a typo can never loosen the gate.

One approval authorises exactly one execution of exactly the text that was shown.
There is no "always allow". Approval commands are only honoured when they arrive
through the Telegram listener, which already verifies the sender is TELEGRAM_CHAT_ID.

Every request and outcome is appended to data/approvals.jsonl for audit.

Stdlib only, so it imports (and is unit-testable) without the app's dependencies.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import re
import secrets
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Dict, Optional, Tuple

import contextvars

from src.foundation import audit as foundation_audit
from src.foundation import mission as foundation_mission
from src.foundation import risk as foundation_risk
from src.foundation.risk import RiskTier
from src.foundation.sandbox import SANDBOX_TOOLS as _SANDBOX_TOOLS

logger = logging.getLogger(__name__)

# Set by enforce() when an ALLOWED call must run in the mission sandbox; read exactly once by
# the executor via consume_route(). Context-local, so concurrent tool calls never mix.
_route: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar("aurix_sandbox_route", default=None)


def consume_route() -> Optional[str]:
    """Mission id if the call the gate just allowed must execute in the sandbox, else None."""
    mission_id = _route.get()
    _route.set(None)
    return mission_id

APPROVAL_TIMEOUT_SECONDS = int(os.environ.get("APPROVAL_TIMEOUT_SECONDS", "300"))
MAX_PENDING = int(os.environ.get("APPROVAL_MAX_PENDING", "3"))
# Anti-spam: a model that ignores "denied" will retry. Identical actions are
# auto-denied (no ping) for this long after the owner denied them or let them
# expire, and the owner is pinged at most this many times per hour.
REPEAT_COOLDOWN_SECONDS = int(os.environ.get("APPROVAL_REPEAT_COOLDOWN_SECONDS", "600"))
MAX_REQUESTS_PER_HOUR = int(os.environ.get("APPROVAL_MAX_REQUESTS_PER_HOUR", "10"))
_NO_RETRY = " Do not retry this or an equivalent action. Tell the owner what you wanted to do and wait for their instruction."
# The owner must be able to read the whole action in one Telegram message
# (4096 char limit incl. header). Anything longer is refused rather than
# approved half-seen; the agent is told to split it up.
MAX_REVIEWABLE_CHARS = 3000

VALID_MODES = ("off", "telegram", "all")

# Native tools that change state, run code, send messages, or touch secrets.
# Read-only tools (read_file, web_search, list_emails, search_chats, ...) are
# deliberately not gated.
GATED_TOOLS = frozenset({
    "bash", "python", "write_file",
    "send_email", "reply_to_email",
    "api_call", "app_api",
    "manage_tasks", "manage_skills", "manage_endpoints", "manage_mcp",
    "manage_webhooks", "manage_tokens", "manage_settings", "manage_contact",
    "vault_get", "vault_unlock",
    "download_model", "serve_model", "serve_preset",
    "stop_served_model", "adopt_served_model",
})

# The model can name MCP-qualified twins of the legacy tools directly
# (e.g. mcp__bash__bash). Treat them as the tool they alias.
_MCP_ALIASES = {
    "mcp__bash__bash": "bash",
    "mcp__python__python": "python",
    "mcp__filesystem__write_file": "write_file",
}

# Other MCP tools are gated unless clearly read-only. Unknown names are gated.
_MCP_READ_RE = re.compile(r"(^|_)(list|read|get|search|query|recall|find|show|status|describe)(_|$)")
_MCP_MUTATE_RE = re.compile(
    r"(delete|remove|send|write|exec|run|create|update|set|archive|move|reply|bulk|post|put|kill|drop)"
)


# Harmless system-info commands run without asking (approval fatigue makes owners
# rubber-stamp everything, which defeats the gate). STRICT: a bare command word,
# optionally followed by short flags, and nothing else - no paths, arguments,
# pipes, redirects, substitutions, or chaining. Still written to the audit log.
# APPROVAL_AUTO_ALLOW_READONLY=0 turns this off.
_SAFE_COMMAND_RE = re.compile(
    r"^(?:date|(?:df|free|uptime|uname|nproc|lsblk|lscpu|whoami|id|pwd|hostname)(?: -[A-Za-z]{1,4})*)$"
)


def is_auto_allowed(tool: str, content: str) -> bool:
    if tool != "bash" or os.environ.get("APPROVAL_AUTO_ALLOW_READONLY", "1").strip() == "0":
        return False
    return bool(_SAFE_COMMAND_RE.match((content or "").strip()))


def gate_mode() -> str:
    mode = os.environ.get("APPROVAL_GATE_MODE", "telegram").strip().lower()
    if mode not in VALID_MODES:
        logger.warning("Unrecognised APPROVAL_GATE_MODE=%r; treating as 'all'", mode)
        return "all"
    return mode


def canonical_tool(tool: str) -> str:
    return _MCP_ALIASES.get(tool, tool)


def _is_consequential(tool: str) -> bool:
    if tool in GATED_TOOLS:
        return True
    if tool.startswith("mcp__"):
        name = tool.split("__", 2)[-1].lower()
        read_only = bool(_MCP_READ_RE.search(name)) and not _MCP_MUTATE_RE.search(name)
        return not read_only
    return False


def _session_is_gated(session_id: Optional[str]) -> bool:
    mode = gate_mode()
    if mode == "off":
        return False
    if mode == "all":
        return True
    # The Telegram agent's session ROTATES daily (session_rotation.py) and the listener talks to the live one, so the gate must
    # follow the same pointer - comparing only the frozen env var left every rotated session ungated (found 2026-09-28).
    # If the live pointer cannot be read, fail closed: gate this call.
    telegram_sessions = {os.environ.get("TELEGRAM_AGENT_SESSION_ID", "")}
    try:
        from src.foundation import session_rotation
        telegram_sessions.add(session_rotation.current_session_id() or "")
    except Exception:
        logger.warning("could not read the live Telegram session; gating this call")
        return True
    telegram_sessions.discard("")
    return bool(session_id) and session_id in telegram_sessions


def requires_approval(tool: str, session_id: Optional[str]) -> bool:
    tool = canonical_tool(tool or "")
    return _is_consequential(tool) and _session_is_gated(session_id)


# Not "consequential" in themselves, but their risk is assessed anyway: read_file on
# credentials is HIGH. Plain LOW results for these are not audited (too chatty).
_ASSESSED_READ_TOOLS = frozenset({"read_file", "web_search", "mcp__filesystem__read_file"})


# ---------------------------------------------------------------------------
# Pending state + audit
# ---------------------------------------------------------------------------

@dataclass
class PendingApproval:
    id: str
    tool: str
    preview: str
    session_id: Optional[str]
    owner: Optional[str]
    created: float
    future: "asyncio.Future[bool]"
    key: str = ""
    risk: str = ""
    script_path: str = ""
    script_sha: str = ""
    script_text: str = ""


_pending: Dict[str, PendingApproval] = {}


def _script_target(canon: str, content: str, workspace: Optional[str]):
    """For `python3 <workspace>/x.py [args]`: (path, sha256, text) so the owner reviews the
    CODE, not just the command line. None for anything else."""
    if canon != "bash" or not workspace:
        return None
    try:
        toks = shlex.split((content or "").strip())
    except ValueError:
        return None
    if len(toks) < 2 or os.path.basename(toks[0]) not in ("python", "python3"):
        return None
    path = toks[1]
    if path.startswith("-") or not path.endswith(".py") or not foundation_risk.within_workspace(path, workspace):
        return None
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    return path, hashlib.sha256(data).hexdigest(), data.decode("utf-8", "replace")
_recent_denials: Dict[str, float] = {}   # action key -> when denied/expired
_request_times: list = []                 # when each owner ping was sent
_rate_notice_at: float = 0.0


def _action_key(tool: str, content: str) -> str:
    return hashlib.sha1(f"{tool}\0{' '.join((content or '').split())}".encode()).hexdigest()


_stop_until: float = 0.0


def engage_stop(seconds: int = 120) -> None:
    """STOP is absolute (spec section 2): for `seconds`, every gated action is refused,
    including from an agent loop that was already in flight when the owner said stop."""
    global _stop_until
    _stop_until = time.time() + seconds
    _audit("stop_engaged", seconds=seconds)


def stop_engaged() -> bool:
    return time.time() < _stop_until


def clear_stop() -> None:
    global _stop_until
    _stop_until = 0.0


def reset_state() -> None:
    """Clear pending + anti-spam memory (tests)."""
    global _rate_notice_at, _stop_until
    _pending.clear()
    _recent_denials.clear()
    _request_times.clear()
    _rate_notice_at = 0.0
    _stop_until = 0.0
    foundation_audit._heads.clear()


# notifier(text) -> bool (True = delivered). Replaceable for tests.
Notifier = Callable[[str], Awaitable[bool]]


async def _telegram_notify(text: str, reply_markup: Optional[dict] = None) -> bool:
    from services.telegram.service import TelegramService

    loop = asyncio.get_running_loop()
    if reply_markup:
        result = await loop.run_in_executor(None, lambda: TelegramService().send(text, reply_markup=reply_markup))
    else:
        result = await loop.run_in_executor(None, TelegramService().send, text)
    return bool(result.ok)


def approval_markup(approval_id: str) -> Optional[dict]:
    """One-tap Approve / Deny buttons for the Telegram ping (APPROVAL_BUTTONS=0 disables). The button carries only
    the verb and the 6-hex id; the listener re-checks that the tap came from the owner's own chat."""
    if os.environ.get("APPROVAL_BUTTONS", "1").strip() == "0":
        return None
    return {"inline_keyboard": [[{"text": "✅ Approve", "callback_data": f"approve:{approval_id}"},
                                 {"text": "⛔ Deny", "callback_data": f"deny:{approval_id}"}]]}


_notifier: Notifier = _telegram_notify


def set_notifier(fn: Optional[Notifier]) -> None:
    global _notifier
    _notifier = fn or _telegram_notify


def _audit(event: str, **fields) -> None:
    """Tamper-evident hash-chained record (src/foundation/audit.py). Never raises."""
    foundation_audit.append(event, **fields)


def pending_ids() -> list:
    return sorted(_pending)


# ---------------------------------------------------------------------------
# Requesting approval (agent side)
# ---------------------------------------------------------------------------

def _format_request(p: PendingApproval, timeout: int) -> str:
    body = html.escape(p.preview)
    risk_line = f"<i>Risk: {html.escape(p.risk)}</i>\n" if p.risk else ""
    script_block = (f"<b>Script</b> <code>{html.escape(p.script_path.rsplit('/', 1)[-1])}</code> "
                    f"(sha256 {p.script_sha[:12]}; approval is void if it changes):\n"
                    f"<pre>{html.escape(p.script_text)}</pre>\n") if p.script_text else ""
    return (
        f"\U0001F510 <b>Approval needed</b> [{p.id}]\n"
        f"Tool: <code>{html.escape(p.tool)}</code>\n"
        f"<pre>{body}</pre>\n"
        f"{script_block}"
        f"{risk_line}"
        f"Reply <code>approve {p.id}</code> or <code>deny {p.id}</code> "
        f"(auto-deny in {timeout // 60} min {timeout % 60}s)."
    )


async def _request(tool: str, content: str, session_id, owner, risk_note: str = "",
                   script: Optional[tuple] = None) -> Optional[str]:
    """Return None if approved, else a denial reason. `script` = (path, sha256, text)."""
    timeout = APPROVAL_TIMEOUT_SECONDS
    preview = (content or "").strip()
    reviewable = len(preview) + (len(script[2]) if script else 0)

    if reviewable > MAX_REVIEWABLE_CHARS:
        _audit("denied", tool=tool, reason="too_long", chars=reviewable, session=session_id)
        return (f"Denied: the {tool} action is {reviewable} characters, too long for the "
                f"owner to review on Telegram (limit {MAX_REVIEWABLE_CHARS}). "
                "Split it into smaller steps.")

    # --- anti-spam: none of these ping the owner --------------------------------
    global _rate_notice_at
    now = time.time()
    key = _action_key(tool, preview)

    for k in [k for k, t in _recent_denials.items() if now - t >= REPEAT_COOLDOWN_SECONDS]:
        del _recent_denials[k]
    denied_at = _recent_denials.get(key)
    if denied_at is not None:
        _audit("denied", tool=tool, reason="repeat_after_denial", session=session_id)
        return (f"Denied: this exact action was already refused {int(now - denied_at)}s ago."
                + _NO_RETRY)

    waiting = next((q for q in _pending.values() if q.key == key), None)
    if waiting is not None:
        _audit("denied", tool=tool, reason="duplicate_of_pending", id=waiting.id, session=session_id)
        return (f"Denied: an identical request [{waiting.id}] is already waiting on the owner."
                + _NO_RETRY)

    _request_times[:] = [t for t in _request_times if now - t < 3600]
    if len(_request_times) >= MAX_REQUESTS_PER_HOUR:
        _audit("denied", tool=tool, reason="hourly_limit", session=session_id)
        if now - _rate_notice_at >= 3600:  # tell the owner once per hour, not once per attempt
            _rate_notice_at = now
            try:
                await _notifier(f"⚠ Approval limit reached ({MAX_REQUESTS_PER_HOUR}/hour). "
                                "Further agent actions are being auto-denied until it resets.")
            except Exception:
                pass
        return (f"Denied: too many approval requests this hour ({MAX_REQUESTS_PER_HOUR})."
                + _NO_RETRY)

    if len(_pending) >= MAX_PENDING:
        _audit("denied", tool=tool, reason="too_many_pending", session=session_id)
        return "Denied: too many approval requests are already waiting on the owner." + _NO_RETRY

    approval_id = secrets.token_hex(3)
    while approval_id in _pending:
        approval_id = secrets.token_hex(3)

    loop = asyncio.get_running_loop()
    p = PendingApproval(approval_id, tool, preview, session_id, owner, now,
                        loop.create_future(), key, risk_note,
                        script[0] if script else "", script[1] if script else "",
                        script[2] if script else "")
    _pending[approval_id] = p
    _request_times.append(now)
    _audit("requested", id=approval_id, tool=tool, preview=preview, session=session_id, owner=owner,
           script_sha=p.script_sha or None)

    try:
        text = _format_request(p, timeout)
        markup = approval_markup(approval_id) if _notifier is _telegram_notify else None   # test notifiers take text only
        delivered = await (_notifier(text, markup) if markup else _notifier(text))
        if not delivered:
            _audit("denied", id=approval_id, reason="notify_failed")
            return "Denied: could not reach the owner to ask for approval."

        try:
            approved = await asyncio.wait_for(p.future, timeout=timeout)
        except asyncio.TimeoutError:
            _audit("expired", id=approval_id)
            _recent_denials[key] = time.time()
            try:
                await _notifier(f"⏱ Approval [{approval_id}] expired; the action was NOT run.")
            except Exception:
                pass
            return f"Denied: the owner did not approve within {timeout}s." + _NO_RETRY

        if approved:
            if p.script_path:                       # approval is bound to the code the owner SAW
                try:
                    now_sha = hashlib.sha256(Path(p.script_path).read_bytes()).hexdigest()
                except OSError:
                    now_sha = ""
                if now_sha != p.script_sha:
                    _audit("denied", id=approval_id, reason="script_changed_after_review")
                    _recent_denials[key] = time.time()
                    return ("Denied: the script changed after the owner reviewed it, so the approval "
                            "is void." + _NO_RETRY)
            _audit("approved", id=approval_id)
            return None
        _audit("denied", id=approval_id, reason="owner_denied")
        _recent_denials[key] = time.time()
        return "Denied by the owner." + _NO_RETRY
    except asyncio.CancelledError:
        _audit("cancelled", id=approval_id)
        raise
    finally:
        _pending.pop(approval_id, None)


def _shadow_opa(tier: str, covers: bool, actual: str, tool: str) -> None:
    """Handoff §10 task 4: put this decision to Open Policy Agent as a second opinion and log any disagreement.
    A NO-OP unless AURIX_OPA_MODE=shadow; runs in a worker thread and swallows every error, so it can never change or delay
    what the gate decides. (See src/foundation/policy_opa.py.)"""
    try:
        if os.environ.get("AURIX_OPA_MODE", "off").strip().lower() != "shadow":
            return
        from src.foundation import policy_opa
        asyncio.get_running_loop().run_in_executor(None, policy_opa.shadow, tier, False, covers, tool, actual)
    except Exception:
        pass


async def enforce(tool: str, content: str, session_id: Optional[str] = None,
                  owner: Optional[str] = None) -> Optional[str]:
    """Gate entry point for execute_tool_block.

    Returns None if the call may proceed, or a human-readable denial reason.
    Never raises (other than task cancellation): any internal error denies.
    """
    _route.set(None)                            # never inherit a stale routing decision
    try:
        canon = canonical_tool(tool or "")
        if not _session_is_gated(session_id):
            return None
        if not (_is_consequential(canon) or canon in _ASSESSED_READ_TOOLS):
            return None
        preview = (content or "").strip()[:500]

        if stop_engaged():
            _audit("denied", tool=canon, reason="owner_stop", preview=preview, session=session_id)
            return "Denied: the owner issued STOP. Nothing further will run." + _NO_RETRY

        store = foundation_mission.MissionStore()
        active = store.active()
        if active and active.session_id not in (None, "", session_id):
            active = None                       # a mission only covers its own session
        # A sandboxed mission runs bash/python INSIDE the isolated container. Whether a call is
        # sandboxed is decided here, once, together with the allow decision, and handed to the
        # executor via consume_route() - so it cannot run locally under sandbox-level trust.
        sandbox_route = active.id if (active and active.sandboxed and canon in _SANDBOX_TOOLS) else None
        assessment = foundation_risk.assess_action(canon, content, active.workspace if active else None,
                                                   sandboxed=bool(sandbox_route))

        # CRITICAL: a structurally protected component. No approval or contract unlocks it.
        if assessment.hard_deny:
            _shadow_opa(assessment.tier.name, False, "deny", canon)
            _audit("denied", tool=canon, reason="protected_component",
                   detail=assessment.describe(), preview=preview, session=session_id)
            return (f"Denied: {assessment.reasons[0]}. Protected components can never be modified "
                    "by the agent, with or without approval." + _NO_RETRY)

        # LOW: runs, audited (plain reads are not audited - too chatty).
        if assessment.tier == RiskTier.LOW:
            readonly_off = (canon == "bash"
                            and os.environ.get("APPROVAL_AUTO_ALLOW_READONLY", "1").strip() == "0")
            if not readonly_off:
                if canon not in _ASSESSED_READ_TOOLS:
                    _audit("auto_allowed", tool=canon, preview=preview,
                           risk=assessment.describe(), session=session_id)
                _shadow_opa(assessment.tier.name, False, "allow", canon)
                return None

        # MEDIUM inside an approved, active mission: runs under the contract.
        note = assessment.describe()
        if active:
            decision = foundation_mission.decide(active, canon, content, assessment)
            if decision.allow:
                store.record_tool_call(active)
                _audit("mission_allowed", mission=active.id, tool=canon, tier=assessment.tier.name,
                       preview=preview, session=session_id, sandboxed=bool(sandbox_route))
                _route.set(sandbox_route)
                _shadow_opa(assessment.tier.name, True, "allow", canon)
                return None
            note += f" | outside mission {active.id}: {decision.reason}"

        # Everything else (HIGH always; MEDIUM with no covering mission): ask the owner.
        _shadow_opa(assessment.tier.name, False, "ask", canon)
        script = _script_target(canon, content, active.workspace if active else None)
        result = await _request(canon, content, session_id, owner, risk_note=note, script=script)
        if result is None:
            _route.set(sandbox_route)           # approved: still runs where the mission says
        return result
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error("approval gate error for tool=%s: %r", tool, e)
        _audit("denied", tool=tool, reason=f"gate_error:{e!r}", session=session_id)
        return "Denied: the approval gate hit an internal error, so the action was not run."


# ---------------------------------------------------------------------------
# Resolving approval (owner side, via the Telegram listener)
# ---------------------------------------------------------------------------

_COMMAND_RE = re.compile(r"^\s*/?(approve|deny)\s+([0-9a-f]{6}|all)\s*$", re.IGNORECASE)


def parse_approval_command(text: str) -> Optional[Tuple[str, str]]:
    """Return (verb, id_or_all) for `approve <id>` / `deny <id|all>`, else None.

    An ID is always required to approve; `approve all` is rejected. A bare
    "yes"/"ok" is never treated as approval so an answer to the agent's own
    conversational question can't approve a held action by accident.
    """
    m = _COMMAND_RE.match(text or "")
    if not m:
        return None
    verb, target = m.group(1).lower(), m.group(2).lower()
    if verb == "approve" and target == "all":
        return None
    return verb, target


_BARE_WORD_RE = re.compile(
    r"^\s*(approve|approved|deny|denied|yes|y|yep|yeah|ok|okay|sure|no|n|nope|do it|go ahead|confirm)\s*[.!]*\s*$",
    re.IGNORECASE)


def bare_reply_hint(text: str) -> Optional[str]:
    """If `text` is a bare yes/no/approve word while approvals are pending, return
    a hint telling the owner the exact command to send; else None.

    Such a reply must NOT reach the agent: it would treat "approve" as a new
    instruction and could request the same action again. It also must not be
    accepted as approval - the owner has to name the id.
    """
    if not _pending or not _BARE_WORD_RE.match(text or ""):
        return None
    lines = ["That was not an approval - I need the id. Pending:"]
    for p in _pending.values():
        preview = " ".join(p.preview.split())[:70]
        lines.append(f"  [{p.id}] {p.tool}: {preview}")
    lines.append("Reply `approve <id>` or `deny <id>` (or `deny all`).")
    return "\n".join(lines)


def _settle(p: PendingApproval, approved: bool) -> None:
    def _set():
        if not p.future.done():
            p.future.set_result(approved)
    p.future.get_loop().call_soon_threadsafe(_set)


def handle_command(verb: str, target: str, resolver: str = "telegram") -> str:
    """Apply a parsed owner command. Returns the reply text for the owner."""
    if target == "all":
        ids = list(_pending)
        for i in ids:
            _settle(_pending[i], False)
        _audit("deny_all", ids=ids, resolver=resolver)
        return f"Denied {len(ids)} pending approval(s)." if ids else "Nothing pending."

    p = _pending.get(target)
    if p is None or p.future.done():
        return f"No pending approval [{target}] (expired or already handled)."
    approved = verb == "approve"
    _settle(p, approved)
    _audit("resolved", id=target, approved=approved, resolver=resolver)
    return (f"✅ Approved [{target}] - running it now." if approved
            else f"⛔ Denied [{target}].")
