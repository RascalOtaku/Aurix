"""src/aurix_memory.py - the "AURIX LIVE MEMORY" block injected into chats, from the brain's memory tiers.

The brain (~/ai/brain/memory/{hot,longterm,...}) holds markdown notes with a tiny front matter:

    ---
    tag: health
    tier: hot
    ---
    HEALTH CHECK OK: all 7 scripts pass syntax check.

The first version of the injection took the 5 newest files of each tier. In practice a supervisor job wrote a
"HEALTH CHECK OK" note every 30 minutes, so the hot tier held ~3,200 of them and every prompt began with five
copies of the same line. This module picks memory that is actually worth a prompt's tokens:

  * skips operational noise (tags: health, syscheck, heartbeat) - they belong in logs, not in the model's context
  * de-duplicates identical notes (whitespace/case-insensitive) across tiers
  * only looks at the newest `scan` files per tier (a huge directory never slows a chat down)
  * clips each note and caps the total

Pure functions over a directory; no network, no model. Unit-tested (tests/test_aurix_memory.py).
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Dict, List, Tuple

NOISE_TAGS = {"health", "syscheck", "heartbeat", "healthcheck"}
DEFAULT_TIERS = ("hot", "longterm")
# "hot" is short-term memory: a note older than this is history, not context (it was injecting 4-month-old daily
# reports). "longterm" is durable by design and has no age limit.
DEFAULT_MAX_AGE_DAYS = {"hot": 30}


def parse_note(text: str) -> Tuple[Dict[str, str], str]:
    """(front-matter dict, body). Tolerates a missing or malformed front matter (whole text is the body)."""
    lines = (text or "").splitlines()
    meta: Dict[str, str] = {}
    if lines and lines[0].strip() == "---":
        for i in range(1, min(len(lines), 40)):
            if lines[i].strip() == "---":
                for raw in lines[1:i]:
                    key, sep, value = raw.partition(":")
                    if sep:
                        meta[key.strip().lower()] = value.strip()
                return meta, "\n".join(lines[i + 1:])
    return meta, "\n".join(lines)


def _norm(body: str) -> str:
    return re.sub(r"\s+", " ", body).strip().lower()


_WORD = re.compile(r"[a-z0-9][a-z0-9'-]{3,}")
_STOP = {"this", "that", "what", "with", "have", "from", "your", "about", "would", "could", "should", "there", "their", "they", "them", "then", "than",
         "when", "where", "which", "will", "been", "were", "does", "just", "like", "some", "more", "also", "into", "want", "need", "please", "thing"}


def _content_words(text: str) -> set:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP}


def relevant(chunk: str, query: str, min_shared: int = 2) -> bool:
    """A note earns its place in the prompt only if it shares real words with what you just said."""
    q = _content_words(query)
    return len(q & _content_words(chunk)) >= min(min_shared, max(1, len(q)))


def recent_memory(brain: Path, tiers=DEFAULT_TIERS, per_tier: int = 5, max_chars: int = 400,
                  scan: int = 80, max_total_chars: int = 2400, max_age_days=None, now=None, query: str = None) -> List[str]:
    """Up to `per_tier` useful, distinct notes from each tier (newest first), each clipped to `max_chars`."""
    max_age_days = DEFAULT_MAX_AGE_DAYS if max_age_days is None else max_age_days
    now = time.time() if now is None else now
    seen: set = set()
    out: List[str] = []
    total = 0
    for tier in tiers:
        directory = Path(brain) / "memory" / tier
        try:
            files = sorted((p for p in directory.glob("*.md") if p.is_file()),
                           key=lambda p: p.stat().st_mtime, reverse=True)[:scan]
        except OSError:
            continue
        taken = 0
        for path in files:
            if taken >= per_tier or total >= max_total_chars:
                break
            try:
                limit = max_age_days.get(tier)
                if limit is not None and now - path.stat().st_mtime > limit * 86400:
                    break                      # files are newest-first: everything after this is older still
                meta, body = parse_note(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            if meta.get("tag", "").lower() in NOISE_TAGS:
                continue
            body = " ".join(line.strip() for line in body.splitlines() if line.strip())
            key = _norm(body)
            if not key or key in seen:
                continue
            if query is not None and not relevant(body, query):
                continue
            seen.add(key)
            chunk = body[:max_chars]
            out.append(chunk)
            taken += 1
            total += len(chunk)
    return out


def live_memory_message(brain: Path, query: str = None) -> str:
    """The system-message text, or '' when there is nothing worth injecting. With a query, only notes that share real words with it are used
    (before: the newest five of each tier went into EVERY prompt, mostly auto-written trading summaries)."""
    chunks = recent_memory(brain, query=query)
    return ("AURIX LIVE MEMORY:\n" + "\n---\n".join(chunks)) if chunks else ""
