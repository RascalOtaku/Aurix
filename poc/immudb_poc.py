"""Spec §10 task 5: "Audit event schema + immudb write path - one real consequential action produces one real structured,
tamper-evident record."  Here: the REAL audit chain head is witnessed in a REAL immudb, with client-side proof verification, and
the two attacks the hash chain alone cannot see (tail truncation, consistent history rewrite) are shown to be caught.
Runs in the client container after `pip install immudb-py`.
"""
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, "/app/scripts")
from src.foundation import audit           # noqa: E402
import audit_witness                        # noqa: E402

failures = 0
REAL = "/audit/audit.jsonl"


def check(label, ok, detail=""):
    global failures
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    failures += 0 if ok else 1


def rewrite_consistently(src, dst, at_seq):
    """What a competent attacker does: alter one record, then recompute every hash after it so the chain still verifies."""
    raw = [json.loads(l) for l in open(src, encoding="utf-8") if l.strip()]
    recs = [r for i, r in enumerate(raw) if i + 1 == len(raw) or raw[i + 1]["seq"] != r["seq"]]   # a forger just drops the orphan of a fork
    prev = audit.GENESIS
    with open(dst, "w", encoding="utf-8") as out:
        for r in recs:
            r.pop("hash", None)
            if r["seq"] == at_seq:
                r["event"] = "nothing_happened_here"            # the falsified history
            r["prev"] = prev
            r["hash"] = audit._digest(prev, r)
            prev = r["hash"]
            out.write(audit._canon(r) + "\n")


def main():
    from immudb import ImmudbClient
    print("== immudb: connect and write")
    client = None
    for _ in range(40):
        try:
            client = ImmudbClient(os.environ.get("IMMUDB_HOST", "immudb") + ":3322")
            client.login("immudb", os.environ["IMMUDB_ADMIN_PASSWORD"])
            break
        except Exception:
            client = None
            time.sleep(1.5)
    check("logged in to immudb with the generated (not default) admin password", client is not None)
    if client is None:
        return
    check("the DEFAULT password no longer works", _default_login_fails())

    seq, head = audit_witness.audit_head(REAL)
    check("the real audit log has a head to witness", seq > 0 and len(head) == 64, f"seq {seq}, hash {head[:12]}...")
    pub = audit_witness.publish(client, REAL)
    check("the head is written with a client-VERIFIED cryptographic proof (verifiedSet)", pub["verified"] and pub["seq"] == seq)
    got = audit_witness.witnessed_head(client)
    check("reading it back (verifiedGet) returns exactly what was witnessed, proof verified", got == (seq, head, True), str(got))

    print("== the real log, today")
    res = audit_witness.check(client, REAL)
    check("the real audit log matches its witness", res["status"] == "ok", str(res))
    real_v = audit.verify(REAL)
    check("the hash chain itself also verifies (independent check; includes the owner-acknowledged fork)", real_v.ok, f"{real_v.records} records, acknowledged {real_v.acknowledged}")

    print("== attack 1: truncate the tail (the chain still verifies!)")
    tmp = tempfile.mkdtemp(prefix="witness_")
    if os.path.exists("/audit/audit_ack.json"):
        shutil.copy("/audit/audit_ack.json", os.path.join(tmp, "audit_ack.json"))            # verify() looks for it beside the log
    truncated = os.path.join(tmp, "truncated.jsonl")
    lines = [l for l in open(REAL, encoding="utf-8") if l.strip()]
    open(truncated, "w", encoding="utf-8").writelines(lines[:-3])
    v = audit.verify(truncated)
    check("the hash chain ALONE cannot see it (the truncated file still verifies)", v.ok, f"verify.ok={v.ok}, {v.records} records")
    res = audit_witness.check(client, truncated)
    check("the immudb WITNESS catches it: tail_truncated", res["status"] == "tail_truncated", res.get("detail", ""))

    print("== attack 2: rewrite history consistently (recompute every hash)")
    rewritten = os.path.join(tmp, "rewritten.jsonl")
    at = max(1, seq - 5)
    rewrite_consistently(REAL, rewritten, at)
    v = audit.verify(rewritten)
    check("the hash chain ALONE cannot see it (a perfectly consistent forged chain verifies)", v.ok, f"verify.ok={v.ok}")
    res = audit_witness.check(client, rewritten)
    check("the immudb WITNESS catches it: rewritten", res["status"] == "rewritten", res.get("detail", ""))

    print("== immutability: history cannot be erased")
    client.verifiedSet(b"audit-head", b"999999:" + b"0" * 64)                 # someone overwrites the 'latest' key...
    hist = client.history(b"audit-head", 0, 20, True)
    values = []
    for h in hist:
        try:
            values.append(audit_witness._value_of(h))
        except ValueError:
            pass
    check("...but every earlier witnessed value is still in the key's history", any(v.decode().startswith(f"{seq}:") for v in values), f"{len(values)} versions")
    per_seq = client.verifiedGet(f"audit-head:{seq}".encode())
    check("and the per-seq witness record is untouched and verifies", audit_witness._value_of(per_seq).decode() == f"{seq}:{head}")
    shutil.rmtree(tmp, ignore_errors=True)
    client.logout()


def _default_login_fails():
    from immudb import ImmudbClient
    try:
        c = ImmudbClient(os.environ.get("IMMUDB_HOST", "immudb") + ":3322")
        c.login("immudb", "immudb")
        return False
    except Exception:
        return True


if __name__ == "__main__":
    try:
        main()
    except Exception as e:                                                      # noqa: BLE001
        print(f"  FAIL  unexpected error: {type(e).__name__}: {e}")
        failures += 1
    print(f"immudb: {'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    sys.exit(1 if failures else 0)
