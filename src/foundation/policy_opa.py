"""src/foundation/policy_opa.py - Open Policy Agent as a SECOND OPINION on the approval gate (Activation Handoff §10 task 4).

The gate in src/approval_gate.py decides in code. The spec wants the policy to live in a policy engine (OPA) so it is
declarative and auditable. Swapping a working safety mechanism in one step would be reckless, so this is staged:

    AURIX_OPA_MODE=off      (default) OPA is never contacted. Zero effect.
    AURIX_OPA_MODE=shadow   the gate still decides alone; every decision is also put to OPA and any DISAGREEMENT is written to
                            the audit log (`opa_disagreement`). Nothing OPA says can change an outcome.
    AURIX_OPA_MODE=enforce  `consult()` returns the STRICTER of the two answers (deny > ask > allow). Not wired into the gate:
                            changing what the gate enforces is the owner's decision (protected component).

Fail-closed everywhere: if OPA is down, slow or answers nonsense, the code decision stands and nothing is loosened - OPA can only
ever make an outcome STRICTER, never looser. The policy text is poc/policies/aurix.rego.

Stdlib only (urllib). Protected component (approval_gate).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from src.foundation import audit

DECISION_PATH = "/v1/data/aurix/approval/decision"
TIERS = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
ACTIONS = ("allow", "ask", "deny")
_STRICTNESS = {"allow": 0, "ask": 1, "deny": 2}
_last_unavailable_note = {"at": 0.0}


def mode() -> str:
    m = os.environ.get("AURIX_OPA_MODE", "off").strip().lower()
    return m if m in ("off", "shadow", "enforce") else "off"


def opa_url() -> str:
    return os.environ.get("OPA_URL", "http://opa:8181").rstrip("/")


def build_input(tier: str, stop: bool = False, contract_covers: bool = False) -> Dict[str, Any]:
    """The policy input, from what the gate already computed. `tier` is a RiskTier name."""
    return {"stop": bool(stop), "tier": str(tier).upper(), "contract_covers": bool(contract_covers)}


def code_decision(inp: Dict[str, Any]) -> str:
    """What approval_gate.enforce does for a gated session, as a pure function (the reference the policy is compared with).

    Written to agree with poc/policies/aurix.rego on EVERY input, malformed ones included (that agreement is fuzz-tested against
    a real OPA): only a real boolean `stop`/`contract_covers` and an exact-case tier name count; anything else asks."""
    stop = inp.get("stop")
    if stop is True:
        return "deny"
    tier = inp.get("tier")
    if tier == "CRITICAL":
        return "deny"
    if stop is not False:                                      # unknown STOP state: maximum caution
        return "ask"
    if tier == "LOW":
        return "allow"
    if tier == "MEDIUM" and inp.get("contract_covers") is True:
        return "allow"
    return "ask"


def query(inp: Dict[str, Any], url: Optional[str] = None, timeout: float = 1.0) -> Optional[Dict[str, str]]:
    """OPA's decision for `inp`, or None on ANY problem (down, slow, HTTP error, malformed answer)."""
    req = urllib.request.Request((url or opa_url()) + DECISION_PATH, data=json.dumps({"input": inp}).encode("utf-8"),
                                 method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read(65536))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    result = body.get("result") if isinstance(body, dict) else None
    if not isinstance(result, dict) or result.get("action") not in ACTIONS:
        return None
    return {"action": result["action"], "reason": str(result.get("reason", ""))[:200]}


def stricter(a: str, b: str) -> str:
    """The stricter of two answers (deny > ask > allow). An unrecognised answer counts as 'ask' - it can never leak out as-is."""
    a = a if a in ACTIONS else "ask"
    b = b if b in ACTIONS else "ask"
    return a if _STRICTNESS[a] >= _STRICTNESS[b] else b


def consult(inp: Dict[str, Any], tool: str = "", url: Optional[str] = None, actual: Optional[str] = None) -> Dict[str, Any]:
    """{'mode', 'code', 'opa' (or None), 'final'}. `final` equals the code decision unless mode is 'enforce' and OPA is stricter.
    `actual` is what the gate really did (it has special cases the pure function does not model); it wins over the recomputation."""
    m = mode()
    code = actual if actual in ACTIONS else code_decision(inp)
    out: Dict[str, Any] = {"mode": m, "code": code, "opa": None, "final": code}
    if m == "off":
        return out
    got = query(inp, url)
    if got is None:
        now = time.time()
        if now - _last_unavailable_note["at"] > 600:                     # one note per 10 minutes, never a flood
            _last_unavailable_note["at"] = now
            audit.append("opa_unavailable", mode=m)
        return out
    out["opa"] = got["action"]
    if got["action"] != code:
        audit.append("opa_disagreement", tool=tool[:60], tier=inp.get("tier"), code=code, opa=got["action"],
                     reason=got["reason"][:120], mode=m)
    if m == "enforce":
        out["final"] = stricter(code, got["action"])
    return out


def shadow(tier: str, stop: bool, contract_covers: bool, tool: str = "", actual: Optional[str] = None) -> None:
    """Fire-and-forget hook for the gate: put the decision to OPA and record any disagreement with what the gate ACTUALLY did.
    Never raises, never blocks the caller's decision (the gate runs this in a worker thread)."""
    try:
        if mode() != "shadow":
            return
        consult(build_input(tier, stop, contract_covers), tool, actual=actual)
    except Exception:
        pass
