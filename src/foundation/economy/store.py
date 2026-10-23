"""economy/store.py - the Economic Engine's single source of truth: one SQLite file.

    $AURIX_PROJECT_ROOT/data/economy/economy.db

Financial numbers and state changes live here as structured rows - never in a vector store or a model's memory. Models may read these
rows and suggest; only code (on the owner's commands and approvals) writes them. Protected component: agent tools cannot write
data/economy/ (identity.PROTECTED_COMPONENTS), so no agent can forge a sale, an approval or a stage change.
"""
from __future__ import annotations

import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS profile (
    key   TEXT PRIMARY KEY,
    value REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS opportunities (
    id                TEXT PRIMARY KEY,          -- a short slug: research_reports
    name              TEXT NOT NULL,
    lane              TEXT NOT NULL,             -- research | automation | products | software | land | other
    problem           TEXT NOT NULL DEFAULT '',  -- the customer problem
    customer          TEXT NOT NULL DEFAULT '',
    stage             TEXT NOT NULL DEFAULT 'discovered',
    origin            TEXT NOT NULL DEFAULT 'owner',   -- seed | catalog | owner | aurix
    startup_cash      REAL NOT NULL DEFAULT 0,   -- cash that could be lost before validation
    max_loss          REAL NOT NULL DEFAULT 0,   -- maximum credible loss, liabilities included
    months_to_revenue REAL NOT NULL DEFAULT 3,
    net_low           REAL NOT NULL DEFAULT 0,   -- PROJECTED monthly net contribution (an assumption until measured)
    net_high          REAL NOT NULL DEFAULT 0,
    owner_hours       REAL NOT NULL DEFAULT 5,   -- hours of the owner's time per week
    automation        REAL NOT NULL DEFAULT 0.3, -- 0..1 share of the work that can be safely automated
    durability        REAL NOT NULL DEFAULT 0.5, -- 0..1 resistance to competitors, platforms, rule changes
    reversibility     REAL NOT NULL DEFAULT 0.8, -- 0..1 how cheaply the experiment can be stopped
    strategic         REAL NOT NULL DEFAULT 0.5, -- 0..1 reusable tech, knowledge, customers or assets it creates
    budget            REAL NOT NULL DEFAULT 0,   -- cash the owner approved for the current stage
    next_action       TEXT NOT NULL DEFAULT '',
    deadline          TEXT NOT NULL DEFAULT '',  -- YYYY-MM-DD for the current stage
    success_criteria  TEXT NOT NULL DEFAULT '',
    stop_conditions   TEXT NOT NULL DEFAULT '',
    notes             TEXT NOT NULL DEFAULT '',
    created           REAL NOT NULL,
    updated           REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
    id       TEXT PRIMARY KEY,
    opp_id   TEXT NOT NULL REFERENCES opportunities(id),
    kind     TEXT NOT NULL,
    summary  TEXT NOT NULL,
    source   TEXT NOT NULL DEFAULT '',
    at       REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS stage_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    opp_id     TEXT NOT NULL,
    from_stage TEXT NOT NULL,
    to_stage   TEXT NOT NULL,
    by         TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    at         REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS forecasts (
    opp_id            TEXT NOT NULL,
    at                REAL NOT NULL,
    net_mid           REAL NOT NULL,
    owner_hours       REAL NOT NULL,
    months_to_revenue REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions (
    id      TEXT PRIMARY KEY,
    opp_id  TEXT,                                -- NULL = not tied to a venture
    kind    TEXT NOT NULL,                       -- revenue | cost | distribution
    status  TEXT NOT NULL,                       -- projected | booked | collected   (costs are always 'collected' = paid)
    amount  REAL NOT NULL,
    note    TEXT NOT NULL DEFAULT '',
    at      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS hours (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    opp_id  TEXT,
    hours   REAL NOT NULL,
    note    TEXT NOT NULL DEFAULT '',
    at      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    id        TEXT PRIMARY KEY,                  -- x-abc123
    opp_id    TEXT NOT NULL,
    action    TEXT NOT NULL,                     -- approve | launch | scale
    to_stage  TEXT NOT NULL,
    cost      REAL NOT NULL DEFAULT 0,
    card      TEXT NOT NULL,                     -- the exact text the owner saw
    status    TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | denied | expired
    created   REAL NOT NULL,
    resolved  REAL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def db_path() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data" / "economy" / "economy.db"


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(3)}"


def now() -> float:
    return time.time()


def rows(sql: str, args: tuple = ()) -> List[Dict[str, Any]]:
    with connect() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def one(sql: str, args: tuple = ()) -> Optional[Dict[str, Any]]:
    r = rows(sql, args)
    return r[0] if r else None


def execute(sql: str, args: tuple = ()) -> int:
    with connect() as c:
        return c.execute(sql, args).rowcount


def meta_get(key: str, default: str = "") -> str:
    r = one("SELECT value FROM meta WHERE key = ?", (key,))
    return r["value"] if r else default


def meta_set(key: str, value: str) -> None:
    execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
