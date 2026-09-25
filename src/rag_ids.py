"""src/rag_ids.py - stable ids for RAG chunks.

The vector store used to name chunks `doc_{hash(text) % 10**16}`. Python's built-in hash() of a str is
randomised per process (PYTHONHASHSEED), so the "already indexed?" check only worked inside one run: after
every restart, re-indexing the same folder added every chunk again. It also ignored the owner and source, so
two users with identical text collided and the second one silently got nothing.

The id is now a SHA-256 over owner + source + chunk position + text: the same chunk always gets the same id
(re-indexing is a no-op), and different owners/sources never collide.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, Optional

_SEP = "\x1f"


def stable_doc_id(text: str, metadata: Optional[Dict[str, Any]] = None) -> str:
    meta = metadata or {}
    key = _SEP.join([str(meta.get("owner", "")), str(meta.get("source", "")), str(meta.get("chunk_id", "")), text or ""])
    return "doc_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
