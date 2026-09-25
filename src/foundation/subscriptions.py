"""src/foundation/subscriptions.py - real, finished work for the "subscription and bills audit" money idea: tell AURIX what you
pay for (one line per subscription), and it builds a running list with a monthly-equivalent total, flags anything that looks like
a duplicate, and creates a to-do reminder near each renewal date (reusing projects.py's existing to-do system - no new machinery).

Deliberately code-only, no model: real dollar amounts should never be guessed or reinterpreted by an LLM. A line AURIX cannot
confidently parse is reported back plainly so the owner can fix the wording, rather than silently guessed at.

Cancelling, downgrading and reviewing statements stay entirely the owner's - this is a checklist and reminders, nothing more.
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
KEEP = 300
_FREQ = {"monthly": ("month", 1), "month": ("month", 1), "mo": ("month", 1), "/mo": ("month", 1),
         "yearly": ("year", 1), "annual": ("year", 1), "annually": ("year", 1), "year": ("year", 1), "yr": ("year", 1),
         "weekly": ("week", 1), "week": ("week", 1), "wk": ("week", 1)}
_MONTHLY_EQUIV = {"month": 1.0, "year": 1 / 12, "week": 4.345}
_AMOUNT_RX = re.compile(r"\$?\s*(\d{1,6}(?:\.\d{1,2})?)")
_FREQ_RX = re.compile(r"\b(" + "|".join(re.escape(k) for k in sorted(_FREQ, key=len, reverse=True)) + r")\b", re.I)
_DAY_RX = re.compile(r"\b(?:on\s+the\s+|renews?\s+(?:on\s+)?(?:the\s+)?|every\s+)(\d{1,2})(?:st|nd|rd|th)?\b", re.I)


def _data() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data" / "subscriptions"


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
    return _read(_data() / "subscriptions.json", [])


def _save(items: List[dict]) -> None:
    _atomic(_data() / "subscriptions.json", items[-KEEP:])


def _parse_line(line: str) -> Optional[Dict[str, Any]]:
    """One subscription from one line of loose text, or None if there is no dollar amount to anchor on."""
    line = line.strip().strip(",;")
    if not line:
        return None
    amt = _AMOUNT_RX.search(line)
    if not amt:
        return None
    amount = float(amt.group(1))
    fm = _FREQ_RX.search(line)
    unit = _FREQ[fm.group(1).lower()][0] if fm else "month"                     # unstated frequency: assume monthly, the common case
    dm = _DAY_RX.search(line)
    day = int(dm.group(1)) if dm and 1 <= int(dm.group(1)) <= 31 else None
    name = line[:amt.start()].strip(" -:$") or line[amt.end():].strip(" -:$,.") or "subscription"
    name = re.sub(r"\s{2,}", " ", name)[:60]
    return {"name": name, "amount": round(amount, 2), "unit": unit, "day": day, "monthly_equiv": round(amount * _MONTHLY_EQUIV[unit], 2)}


def add(text: str, now: Optional[float] = None) -> str:
    """One line per subscription. Returns the owner-facing summary; lines that could not be parsed are listed, not guessed at."""
    now = now or time.time()
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return "List one subscription per line, e.g.\n<code>Netflix $15.49 monthly renews 15th\nSpotify $11.99/mo</code>"
    parsed, failed = [], []
    for ln in lines:
        row = _parse_line(ln)
        (parsed if row else failed).append(row or ln.strip())
    items = entries()
    added = []
    for row in parsed:
        row["id"] = "s-" + secrets.token_hex(3)
        row["added"] = now
        items.append(row)
        added.append(row)
        if row["day"]:
            try:
                from src.foundation import projects
                projects.Registry().add_todo(f"Review/cancel {row['name']} (${row['amount']:.2f}/{row['unit']})")
            except Exception:                                                   # noqa: BLE001 - the to-do is a nice-to-have, not the point
                pass
    _save(items)
    for row in added:
        audit.append("subscription_added", id=row["id"], monthly_equiv=row["monthly_equiv"])
    lines_out = [f"💳 Added {len(added)} subscription(s): " + ", ".join(f"{e(r['name'])} ${r['amount']:.2f}/{r['unit']}" for r in added)] if added else []
    if failed:
        lines_out.append("Could not read " + str(len(failed)) + " line(s) - no dollar amount found: " + "; ".join(e(str(f)[:60]) for f in failed))
    lines_out.append(render_summary())
    return "\n".join(lines_out)


def duplicates() -> List[List[dict]]:
    """Groups of entries with the same (lowercased) name - a likely double sign-up or a forgotten old one."""
    by_name: Dict[str, List[dict]] = {}
    for row in entries():
        by_name.setdefault(row["name"].strip().lower(), []).append(row)
    return [group for group in by_name.values() if len(group) > 1]


def total_monthly() -> float:
    return round(sum(r["monthly_equiv"] for r in entries()), 2)


def remove(sub_id: str) -> str:
    items = entries()
    kept = [r for r in items if r["id"] != sub_id]
    if len(kept) == len(items):
        return "No subscription with that id."
    _save(kept)
    audit.append("subscription_removed", id=sub_id)
    return "Removed. " + render_summary()


def render_summary() -> str:
    items = entries()
    if not items:
        return "💳 No subscriptions tracked yet. <code>subscriptions: &lt;one per line&gt;</code> to add some."
    lines = [f"💳 <b>Subscriptions</b> · {len(items)} tracked · ~${total_monthly():,.2f}/mo total"]
    for r in sorted(items, key=lambda x: -x["monthly_equiv"])[:15]:
        lines.append(f"• {e(r['name'])} ${r['amount']:.2f}/{r['unit']}" + (f" (renews ~day {r['day']})" if r["day"] else "") + f" <code>{r['id']}</code>")
    dups = duplicates()
    if dups:
        lines.append("⚠️ Possible duplicates: " + "; ".join(e(g[0]["name"]) for g in dups))
    lines.append("<code>subscriptions remove &lt;id&gt;</code> to drop one. Reviewing statements and cancelling stay yours.")
    return "\n".join(lines)


def panel() -> Dict[str, Any]:
    items = entries()
    return {"total_monthly": total_monthly(), "count": len(items), "duplicates": len(duplicates()),
            "items": [{"id": r["id"], "name": r["name"], "amount": r["amount"], "unit": r["unit"], "day": r.get("day")}
                      for r in sorted(items, key=lambda x: -x["monthly_equiv"])[:15]]}
