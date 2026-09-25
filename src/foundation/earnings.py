"""src/foundation/earnings.py - one ledger for every dollar AURIX's money workstreams touch. AURIX never holds, moves, or receives money -
this is purely a running record so "approve, then see money come in" has something real to look at, the same shape as the paper-trading
ledger (real ledger, honest math, no pretending).

Two kinds of entry:
  * ESTIMATED - logged automatically when a workstream finishes real, deliverable work (e.g. a transcription job), using the same rough
    per-unit rates money.py already shows the owner. Not a promise: it is what the work is roughly worth if delivered and paid for.
  * CONFIRMED - the owner tells AURIX a payment actually landed (`earned <amount> <source>`). Only these count toward a total you could
    call "income"; estimated entries are kept separate and always labelled as such.
"""
from __future__ import annotations

import html
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.foundation import audit

e = html.escape
KEEP_ENTRIES = 500


def _data() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data" / "earnings"


def _atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def entries() -> List[dict]:
    return _read(_data() / "ledger.json", [])


def _add(amount: float, source: str, note: str, confirmed: bool, now: Optional[float]) -> dict:
    now = now or time.time()
    row = {"id": ("c-" if confirmed else "e-") + secrets.token_hex(3), "amount": round(float(amount), 2), "source": source[:40],
           "note": note[:200], "confirmed": confirmed, "at": now}
    items = entries()
    items.append(row)
    _atomic(_data() / "ledger.json", items[-KEEP_ENTRIES:])
    audit.append("earnings_confirmed" if confirmed else "earnings_estimated", id=row["id"], source=source[:40], amount=row["amount"])
    return row


def record(amount: float, source: str, note: str = "") -> str:
    """The owner reporting real income. Returns the owner-facing confirmation text."""
    if amount <= 0:
        return "That has to be a positive amount."
    row = _add(amount, source, note, confirmed=True, now=None)
    return f"💰 Logged <b>${row['amount']:,.2f}</b> from <b>{e(source)}</b>{(' - ' + e(note)) if note else ''}. Confirmed total: ${total(confirmed_only=True):,.2f}."


def auto_estimate(amount: float, source: str, note: str = "", now: Optional[float] = None) -> dict:
    """A workstream logging what its own finished work is roughly worth - never a promise, never money AURIX touched."""
    return _add(amount, source, note, confirmed=False, now=now)


def total(confirmed_only: bool = False) -> float:
    return round(sum(x["amount"] for x in entries() if x["confirmed"] or not confirmed_only), 2)


def by_source(confirmed_only: bool = False) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for x in entries():
        if confirmed_only and not x["confirmed"]:
            continue
        out[x["source"]] = round(out.get(x["source"], 0.0) + x["amount"], 2)
    return out


def render() -> str:
    items = entries()
    if not items:
        return ("💰 <b>Earnings ledger</b> - nothing logged yet. When a workstream finishes real work (like a transcription) it logs "
                "what that work is roughly worth; tell AURIX <code>earned &lt;amount&gt; &lt;source&gt;</code> when a payment actually lands.")
    confirmed, estimated = total(confirmed_only=True), total() - total(confirmed_only=True)
    lines = [f"💰 <b>Earnings ledger</b> · confirmed <b>${confirmed:,.2f}</b> · estimated (not yet paid) ${estimated:,.2f}"]
    by_c = by_source(confirmed_only=True)
    if by_c:
        lines.append("Confirmed by source: " + ", ".join(f"{e(k)} ${v:,.2f}" for k, v in sorted(by_c.items(), key=lambda kv: -kv[1])))
    for x in sorted(items, key=lambda r: -r["at"])[:8]:
        tag = "✅" if x["confirmed"] else "~"
        lines.append(f"{tag} ${x['amount']:,.2f} <b>{e(x['source'])}</b>{(' - ' + e(x['note'])) if x['note'] else ''}")
    lines.append("<i>Estimated entries (~) are what finished work is roughly worth, not money received. "
                  "<code>earned &lt;amount&gt; &lt;source&gt;</code> logs a real payment.</i>")
    return "\n".join(lines)


def panel() -> Dict[str, Any]:
    items = entries()
    return {"confirmed_total": total(confirmed_only=True), "estimated_total": round(total() - total(confirmed_only=True), 2),
            "by_source": by_source(confirmed_only=True),
            "recent": [{"id": x["id"], "amount": x["amount"], "source": x["source"], "note": x["note"], "confirmed": x["confirmed"], "at": x["at"]}
                       for x in sorted(items, key=lambda r: -r["at"])[:15]]}
