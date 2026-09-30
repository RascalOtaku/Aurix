"""src/spend_ledger.py - a durable ledger of PAID model tokens, with a monthly circuit breaker.

The best of Paperclip ("monthly budgets per agent; when spending hits the limit, agents stop") and Munder Difflin
("a durable ledger records spending; a circuit breaker prevents runaway consumption"), sized for one owner:

  * every call to a hosted, non-free model is recorded by month / provider / model (input + output tokens);
  * AURIX_MONTHLY_PAID_TOKEN_CAP (tokens, 0/unset = no cap) trips the breaker: further PAID calls fail fast with 402,
    which the existing fallback chains in src/llm_core.py treat like any failure and move on to the next candidate -
    so work continues on local and free models instead of stopping;
  * local endpoints (localhost, LAN, Tailscale 100.64/10, *.local) and free models (model_router.is_free_model)
    are never counted and never blocked.

Stored in data/spend_ledger.json. Tokens, not dollars: prices differ per provider and change often, and the
owner's cap is really "how much hosted usage per month", which tokens express without a price table.
"""
from __future__ import annotations

import ipaddress
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

_lock = threading.Lock()


def _path() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))) \
        / "data" / "spend_ledger.json"


def _month(now: Optional[float] = None) -> str:
    return time.strftime("%Y-%m", time.gmtime(now if now is not None else time.time()))


def cap() -> int:
    try:
        return max(0, int(os.environ.get("AURIX_MONTHLY_PAID_TOKEN_CAP", "0") or 0))
    except ValueError:
        return 0


def is_local(url: str) -> bool:
    host = (urlparse(url or "").hostname or "").lower()
    if not host or host == "localhost" or host.endswith(".local") or host.endswith(".internal") or "." not in host:
        return True                                   # bare names are compose services / LAN hosts
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip in ipaddress.ip_network("100.64.0.0/10")


def is_paid(url: str, model: str) -> bool:
    if is_local(url):
        return False
    from src.model_router import is_free_model
    return not is_free_model(model)


def _read() -> Dict[str, Any]:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(data: Dict[str, Any]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def record(url: str, model: str, provider: str, input_tokens: int, output_tokens: int,
           now: Optional[float] = None) -> None:
    """Add one paid call's usage. Never raises: accounting must not break a reply."""
    try:
        if not is_paid(url, model):
            return
        n_in, n_out = max(0, int(input_tokens or 0)), max(0, int(output_tokens or 0))
        if not (n_in or n_out):
            return
        with _lock:
            data = _read()
            m = data.setdefault(_month(now), {"total": 0, "calls": 0, "by_model": {}})
            m["total"] += n_in + n_out
            m["calls"] += 1
            row = m["by_model"].setdefault(f"{provider}:{model}", {"in": 0, "out": 0, "calls": 0})
            row["in"] += n_in
            row["out"] += n_out
            row["calls"] += 1
            _write(data)
    except Exception:
        pass


def used(now: Optional[float] = None) -> int:
    return int((_read().get(_month(now)) or {}).get("total", 0))


def blocked_reason(url: str, model: str, now: Optional[float] = None) -> str:
    """'' when the call may go ahead; otherwise why the breaker is open."""
    limit = cap()
    if not limit or not is_paid(url, model):
        return ""
    spent = used(now)
    if spent < limit:
        return ""
    return (f"monthly paid-token cap reached ({spent:,} / {limit:,} tokens in {_month(now)}); "
            "local and free models still work. Raise AURIX_MONTHLY_PAID_TOKEN_CAP to lift it.")


def panel(now: Optional[float] = None) -> Dict[str, Any]:
    """Command-center card: this month's paid usage, the cap, and the heaviest models."""
    m = _read().get(_month(now)) or {}
    top = sorted((m.get("by_model") or {}).items(), key=lambda kv: -(kv[1]["in"] + kv[1]["out"]))[:5]
    limit = cap()
    total = int(m.get("total", 0))
    return {"month": _month(now), "tokens": total, "calls": int(m.get("calls", 0)), "cap": limit,
            "pct": round(100.0 * total / limit, 1) if limit else None, "tripped": bool(limit and total >= limit),
            "top": [{"model": k, "tokens": v["in"] + v["out"], "calls": v["calls"]} for k, v in top]}
