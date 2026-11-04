"""economy/check.py - `econ check`: does the DEPLOYED engine behave, not just the tested one?

Tests prove the code. This runs on the live box after a deploy and reports, in one message:
    code          the engine's code fingerprint (compare with the one in the deploy notes) and ENGINE_VERSION
    database      where it is, that it is writable, SQLite's own integrity check, row counts
    settings      every profile value (set by you, or still a placeholder) and the freedom arithmetic worked through
    bookkeeping   a scripted month-by-month scenario (sale, refund, loss month, empty month, tax, booked and projected revenue)
                  replayed on a THROWAWAY database and compared with hand-worked answers - the live ledger is never touched
    approvals     a live round-trip with the build pipeline's own ApprovalManager in a throwaway folder: one manager asks, a second
                  one (as the Telegram listener would) decides, and the first must see the decision; plus the card's buttons
    outbox        quiet hours, and how many held messages are waiting (and how many still carry their buttons)
    schedule      when the next weekly review is due
"""
from __future__ import annotations

import hashlib
import html
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Tuple

from src.foundation.economy import ENGINE_VERSION, freedom, ledger, store

e = html.escape
CODE = ("src/foundation/economy", "src/foundation/build_approvals.py", "src/foundation/outbox.py", "aurix_approval.py")


def fingerprint(root: Path = None) -> str:
    root = root or Path(__file__).resolve().parents[3]
    h = hashlib.sha256()
    for rel in CODE:
        p = root / rel
        files = sorted(p.rglob("*.py")) if p.is_dir() else [p]
        for f in files:
            if f.is_file() and "__pycache__" not in f.parts:
                h.update(f.relative_to(root).as_posix().encode())
                h.update(f.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:12]


def _ok(good: bool) -> str:
    return "✅" if good else "❌"


def _month_ts(months_back: int, now: float, day: int = 10) -> float:
    d = ledger._local(now)
    y, m = d.year, d.month - months_back
    while m <= 0:
        y, m = y - 1, m + 12
    return datetime(y, m, day, 12, tzinfo=timezone.utc).timestamp()


def bookkeeping_selftest(now: float = None) -> List[Tuple[str, bool]]:
    """Replay a known scenario on a scratch database; compare with hand-worked numbers (25% tax, the default)."""
    now = now or store.now()
    results = []
    with tempfile.TemporaryDirectory() as tmp, store.scratch(Path(tmp) / "check.db"):
        m3, m2 = _month_ts(3, now), _month_ts(2, now)
        ledger.add("revenue", 500, None, "collected", "sale", at=m3)          # m-3: 500 in, 100 out -> profit 400, tax 100, available 300
        ledger.add("cost", 100, None, "collected", "costs", at=m3)
        ledger.add("revenue", 1000, None, "projected", "a forecast", at=m3)   # never counts
        ledger.add("revenue", 300, None, "collected", "sale", at=m2)          # m-2: 300 in, 50 refunded, 400 out -> loss of 150, no tax
        ledger.add("refund", 50, None, "collected", "refund", at=m2)
        ledger.add("cost", 400, None, "collected", "costs", at=m2)
        ledger.add("revenue", 200, None, "booked", "invoice", at=m2)          # booked, not collected: never counts
        a3 = ledger.month_summary(ledger.month_of(m3))                        # m-1: nothing at all -> $0
        a2 = ledger.month_summary(ledger.month_of(m2))
        dep, months = ledger.dependable(now)
        results += [("a forecast and a booked sale never count as cash", a3["collected"] == 500 and a2["collected"] == 250),
                    ("tax reserve only on a profitable month (100 / 0)", a3["tax_reserve"] == 100 and a2["tax_reserve"] == 0),
                    ("available after costs and tax (300)", a3["available"] == 300),
                    ("a loss month is shown, not hidden (-150)", a2["available"] == -150),
                    ("an empty month counts as $0", months[0][1] == 0),
                    ("dependable = worst of 3 months, floored at $0", dep == 0),
                    ("one good month is not dependable income", dep < a3["available"])]
        f = freedom.compute(now)
        results.append(("freedom arithmetic on the defaults (2,250 → 2,812.50 · 13,500)",
                        (f["monthly_costs"], f["target"], f["emergency_fund"]) == (2250, 2812.5, 13500)))
    return results


def approval_roundtrip() -> List[Tuple[str, bool]]:
    try:
        from aurix_approval import ApprovalManager
        from aurix_build_brain import RecoveryState
        from src.foundation import build_approvals, buttons
    except Exception as exc:                                       # noqa: BLE001
        return [(f"build pipeline modules importable ({type(exc).__name__})", False)]
    with tempfile.TemporaryDirectory() as tmp:
        brain = ApprovalManager(recovery=RecoveryState(state_dir=tmp))          # the pipeline process
        req = brain.request(action="self-check", target="nothing", reason="econ check round-trip")
        brain.check(req.id)                                                     # warm its in-memory copy, as a polling brain would
        listener = ApprovalManager(recovery=RecoveryState(state_dir=tmp))       # the 7070's Telegram listener
        listener.resolve(req.id, "approved", resolved_by="econ-check")
        seen = brain.check(req.id)
        text, _ = build_approvals.render(req.to_card())
        kb = buttons.for_reply("build_approval", text) or {}
        cmd = buttons.command_from_data(((kb.get("inline_keyboard") or [[{}]])[0][0]).get("callback_data", ""))
    return [("a decision made by another process reaches the waiting pipeline", bool(seen and seen.status == "approved")),
            ("approval cards carry Approve/Deny buttons that parse back to `yes build`", cmd == f"yes build {req.id}")]


def run(now: float = None) -> str:
    now = now or store.now()
    lines = [f"🔎 <b>Economic Engine check</b> · engine {ENGINE_VERSION} · code <code>{fingerprint()}</code>"]
    path = store.db_path()
    try:
        with store.connect() as c:
            integrity = c.execute("PRAGMA integrity_check").fetchone()[0]
            c.execute("CREATE TABLE IF NOT EXISTS _check (x INTEGER)")
            c.execute("DROP TABLE _check")
            counts = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                      for t in ("opportunities", "evidence", "approvals", "transactions", "hours")}
        lines.append(f"{_ok(integrity == 'ok')} <b>Database</b> {e(str(path))}: writable, integrity {e(integrity)} · "
                     + ", ".join(f"{k} {v}" for k, v in counts.items()))
    except (sqlite3.Error, OSError) as exc:
        lines.append(f"❌ <b>Database</b> {e(str(path))}: {e(type(exc).__name__)} {e(str(exc)[:120])}")
    p = freedom.profile()
    ph = freedom.placeholders()
    lines.append("<b>Settings:</b> " + " · ".join(
        f"{k} {v:,.2f}" + ("" if freedom.is_set(k) else " (placeholder)" if k in ph else " (default)") for k, v in p.items()))
    c = freedom.compute(now)
    lines.append(f"<b>Freedom math:</b> {p['essentials']:,.2f} + {p['irregular']:,.2f}/12 = {c['monthly_costs']:,.2f} costs; "
                 f"÷ (1 − {p['margin']:g}%) = {c['target']:,.2f} target; × {p['reserve_months']:g} months = {c['emergency_fund']:,.2f} reserve")
    book = bookkeeping_selftest(now)
    lines.append(f"{_ok(all(g for _, g in book))} <b>Bookkeeping</b> ({sum(g for _, g in book)}/{len(book)} on a scratch ledger):")
    lines += [f"   {_ok(g)} {e(name)}" for name, g in book]
    ap = approval_roundtrip()
    lines.append(f"{_ok(all(g for _, g in ap))} <b>Approvals</b>:")
    lines += [f"   {_ok(g)} {e(name)}" for name, g in ap]
    from src.foundation import build_approvals, outbox
    lines.append(f"<b>Build pipeline folder</b> {e(str(build_approvals.state_dir()))}: "
                 + (f"present, {len(build_approvals.pending())} pending" if build_approvals.present()
                    else "not on this machine (set AURIX_BRAIN_STATE if the brain runs here)"))
    st = outbox._load()
    held = st.get("held", [])
    lines.append(f"<b>Outbox:</b> quiet hours {e(outbox.quiet_window())} · {len(held)} held, "
                 f"{sum(1 for h in held if h.get('buttons'))} with buttons kept")
    ln = ledger._local(now)
    days = (6 - ln.weekday()) % 7
    nxt = (ln + timedelta(days=days)).replace(hour=18, minute=0, second=0, microsecond=0)
    if nxt <= ln:
        nxt += timedelta(days=7)
    review_on = os.environ.get("AURIX_ECON_REVIEW", "1") != "0"
    lines.append("<b>Weekly review:</b> " + (f"next {nxt.strftime('%a %Y-%m-%d %H:%M')} ({ln.tzname() or 'local'})" if review_on
                                               else "off (AURIX_ECON_REVIEW=0)"))
    return "\n".join(lines)
