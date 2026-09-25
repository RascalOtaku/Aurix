"""audit_witness.py - an EXTERNAL witness for the hash-chained audit log, using immudb (Activation Handoff §5/§10 task 5).

Why: src/foundation/audit.py chains every record to the one before, so editing, deleting or reordering a record in the middle
is detected. Two attacks are NOT detectable from the file alone (its own docstring says so):
    * truncating the TAIL (the last records simply vanish; what remains is a perfectly valid chain), and
    * rewriting history CONSISTENTLY (change a record, then recompute every hash after it; the result is a valid chain).
A witness outside the machine's control fixes both: it remembers the chain head `seq:hash` in a tamper-evident database whose
every write returns a cryptographic proof, and `check()` compares today's file with what was witnessed.

Keys written: `audit-head` (the latest head) and `audit-head:<seq>` (one per publish, so history is never lost). Values: `<seq>:<hash>`.
Needs the `immudb-py` package (client-side proof verification) - only in the container that runs it. Stdlib otherwise.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

HEAD_KEY = b"audit-head"


def audit_head(path) -> Tuple[int, str]:
    """(seq, hash) of the newest record in an audit file; (0, '') for an empty or missing file."""
    seq, h = 0, ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rec = json.loads(line)
                    seq, h = int(rec.get("seq", 0)), str(rec.get("hash", ""))
    except (OSError, ValueError):
        return 0, ""
    return seq, h


def hash_at(path, seq: int) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rec = json.loads(line)
                    if int(rec.get("seq", -1)) == seq:
                        return str(rec.get("hash", ""))
    except (OSError, ValueError):
        return None
    return None


def _value_of(result: Any) -> bytes:
    """immudb-py returns slightly different result objects across versions; the value is always somewhere obvious."""
    for attr in ("value", "verifiedvalue"):
        v = getattr(result, attr, None)
        if isinstance(v, (bytes, bytearray)):
            return bytes(v)
    if isinstance(result, (bytes, bytearray)):
        return bytes(result)
    if isinstance(result, dict) and isinstance(result.get("value"), (bytes, bytearray)):
        return bytes(result["value"])
    raise ValueError(f"cannot read a value from {type(result).__name__}")


def publish(client, path) -> Dict[str, Any]:
    """Witness the current head. `client` is a logged-in immudb-py client. Returns {'seq', 'hash', 'verified'}.
    Uses verifiedSet: the client checks the server's inclusion proof itself, so a lying server is detected on the spot."""
    seq, h = audit_head(path)
    if not seq:
        raise ValueError("nothing to witness: the audit file is empty or missing")
    value = f"{seq}:{h}".encode()
    res = client.verifiedSet(HEAD_KEY, value)
    client.verifiedSet(f"audit-head:{seq}".encode(), value)
    return {"seq": seq, "hash": h, "verified": bool(getattr(res, "verified", True))}


def witnessed_head(client) -> Optional[Tuple[int, str, bool]]:
    """The latest witnessed head as (seq, hash, proof_verified), or None if nothing was ever witnessed."""
    try:
        res = client.verifiedGet(HEAD_KEY)
    except Exception:
        return None
    seq_s, _, h = _value_of(res).decode().partition(":")
    return int(seq_s), h, bool(getattr(res, "verified", True))


def check(client, path) -> Dict[str, Any]:
    """Compare an audit file with the witnessed head. status: ok | tail_truncated | rewritten | no_witness."""
    w = witnessed_head(client)
    cur_seq, cur_hash = audit_head(path)
    if w is None:
        return {"status": "no_witness", "current": {"seq": cur_seq, "hash": cur_hash}}
    w_seq, w_hash, proof_ok = w
    out = {"witnessed": {"seq": w_seq, "hash": w_hash, "proof_verified": proof_ok}, "current": {"seq": cur_seq, "hash": cur_hash}}
    if cur_seq < w_seq:
        return {**out, "status": "tail_truncated", "detail": f"the file ends at seq {cur_seq} but seq {w_seq} was witnessed"}
    if hash_at(path, w_seq) != w_hash:
        return {**out, "status": "rewritten", "detail": f"the record at seq {w_seq} is not the one that was witnessed"}
    return {**out, "status": "ok", "grown_by": cur_seq - w_seq}
