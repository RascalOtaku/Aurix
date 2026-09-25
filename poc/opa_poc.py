"""Spec §10 task 4: "OPA policy stub - even a trivial 'always ask' policy wired into one real action path, so the approval-gate
pattern exists structurally from day one."  Here: the REAL policy (poc/policies/aurix.rego) in a REAL OPA server, proven to agree
with the code gate on every input, and wired to the real gate in shadow mode (src/foundation/policy_opa.py).
"""
import itertools
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

from src.foundation import audit, policy_opa

OPA = os.environ.get("OPA_URL", "http://opa:8181")
failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    failures += 0 if ok else 1


def http(method, path, body=None):
    req = urllib.request.Request(OPA + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read() or b"{}")


def audit_events():
    try:
        with open(audit.audit_path(), encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except OSError:
        return []


def main():
    print("== OPA: is the real policy loaded?")
    for _ in range(30):
        try:
            http("GET", "/health")
            break
        except (urllib.error.URLError, OSError):
            time.sleep(1)
    policies = http("GET", "/v1/policies").get("result", [])
    check("OPA is up and has loaded aurix.rego", any("aurix.rego" in p.get("id", "") for p in policies), ", ".join(p["id"] for p in policies))

    print("== OPA: every well-formed input agrees with the code gate")
    tiers = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
    mismatches, table = [], []
    for stop, tier, covers in itertools.product((True, False), tiers, (True, False)):
        inp = policy_opa.build_input(tier, stop, covers)
        got = policy_opa.query(inp, OPA)
        want = policy_opa.code_decision(inp)
        table.append((stop, tier, covers, want, got and got["action"]))
        if not got or got["action"] != want:
            mismatches.append((inp, want, got))
    check("all 16 combinations of (stop, tier, contract covers) match", not mismatches, str(mismatches[:2]) if mismatches else "16/16")
    by = {(s, t, c): w for s, t, c, w, _ in table}
    check("STOP denies everything, even LOW", all(by[(True, t, c)] == "deny" for t in tiers for c in (True, False)))
    check("CRITICAL is denied, even with a covering contract", by[(False, "CRITICAL", True)] == "deny")
    check("HIGH always asks, even with a covering contract", by[(False, "HIGH", True)] == "ask" and by[(False, "HIGH", False)] == "ask")
    check("MEDIUM allows only with a covering contract", by[(False, "MEDIUM", True)] == "allow" and by[(False, "MEDIUM", False)] == "ask")
    check("LOW allows", by[(False, "LOW", False)] == "allow")

    print("== OPA: fail closed on nothing / nonsense")
    check("empty input asks", http("POST", "/v1/data/aurix/approval/decision", {"input": {}})["result"]["action"] == "ask")
    check("no input at all asks", http("POST", "/v1/data/aurix/approval/decision", {})["result"]["action"] == "ask")
    for label, weird in (("lowercase tier", {"stop": False, "tier": "low", "contract_covers": False}),
                         ("string 'true' for stop", {"stop": "true", "tier": "LOW", "contract_covers": False}),
                         ("string 'true' for contract_covers", {"stop": False, "tier": "MEDIUM", "contract_covers": "true"}),
                         ("unknown tier", {"stop": False, "tier": "SUPERHIGH", "contract_covers": True}),
                         ("missing stop state", {"tier": "LOW", "contract_covers": True}),
                         ("null tier", {"stop": False, "tier": None, "contract_covers": True})):
        got = http("POST", "/v1/data/aurix/approval/decision", {"input": weird})["result"]["action"]
        check(f"never allows on {label}", got != "allow", got)

    print("== OPA: differential fuzz - 1000 random inputs, real OPA vs the Python reference")
    rnd = random.Random(20260919)
    pool_stop = [True, False, "true", "false", None, 0, 1, "", "yes"]
    pool_tier = ["LOW", "MEDIUM", "HIGH", "CRITICAL", "low", "Medium", "SUPERHIGH", "", None, 3, ["LOW"], {"a": 1}]
    pool_cov = [True, False, "true", None, 1, 0, "", "yes"]
    disagreements, loosened, n = [], [], 1000
    for _ in range(n):
        inp = {}
        for key, pool in (("stop", pool_stop), ("tier", pool_tier), ("contract_covers", pool_cov)):
            if rnd.random() > 0.08:                                         # sometimes leave the field out entirely
                inp[key] = rnd.choice(pool)
        if rnd.random() < 0.1:
            inp["extra"] = rnd.choice(pool_tier)
        opa_answer = http("POST", "/v1/data/aurix/approval/decision", {"input": inp})["result"]["action"]
        code_answer = policy_opa.code_decision(inp)
        if opa_answer != code_answer:
            disagreements.append((inp, code_answer, opa_answer))
        # the safety property: OPA may never be LOOSER than a fully-proven "allow" would justify
        proven_allow = inp.get("stop") is False and (inp.get("tier") == "LOW" or (inp.get("tier") == "MEDIUM" and inp.get("contract_covers") is True))
        if opa_answer == "allow" and not proven_allow:
            loosened.append((inp, opa_answer))
    check(f"OPA and the code gate agree on all {n} random and malformed inputs", not disagreements, str(disagreements[:2]) if disagreements else f"{n}/{n}")
    check("OPA never says 'allow' unless the input PROVES it", not loosened, str(loosened[:2]) if loosened else "0 violations")

    print("== wired to the gate's decision path (src/foundation/policy_opa.py), in a private audit log")
    os.environ["OPA_URL"] = OPA
    os.environ["AURIX_OPA_MODE"] = "shadow"
    audit._heads.clear()
    before = len(audit_events())
    r = policy_opa.consult(policy_opa.build_input("LOW"), "read_file", actual="allow")
    check("shadow: agreement is silent", r["opa"] == "allow" and r["final"] == "allow" and len(audit_events()) == before)
    r = policy_opa.consult(policy_opa.build_input("HIGH"), "send_email", actual="allow")
    evs = [e for e in audit_events() if e["event"] == "opa_disagreement"]
    check("shadow: if the gate had wrongly allowed a HIGH action, OPA's disagreement is AUDITED", len(evs) == 1 and evs[0]["tool"] == "send_email" and evs[0]["opa"] == "ask", str(evs[-1:]))
    check("shadow: ...but the outcome is NOT changed (the gate still decides alone)", r["final"] == "allow")
    os.environ["AURIX_OPA_MODE"] = "enforce"
    r = policy_opa.consult(policy_opa.build_input("HIGH"), "send_email", actual="allow")
    check("enforce: the STRICTER answer wins (allow vs ask -> ask)", r["final"] == "ask")
    loosen = [(t, c, a) for t in tiers for c in (True, False) for a in ("allow", "ask", "deny")
              if policy_opa._STRICTNESS[policy_opa.consult(policy_opa.build_input(t, False, c), actual=a)["final"]] < policy_opa._STRICTNESS[a]]
    check("enforce can never be LOOSER than the gate's own decision (36 combinations)", not loosen, str(loosen[:2]) if loosen else "0")
    os.environ["AURIX_OPA_MODE"] = "off"
    n_before = len(audit_events())
    r = policy_opa.consult(policy_opa.build_input("HIGH"), "send_email", actual="allow")
    check("off (the default): OPA is not even contacted", r["opa"] is None and len(audit_events()) == n_before)
    os.environ["AURIX_OPA_MODE"] = "shadow"
    os.environ["OPA_URL"] = "http://127.0.0.1:1"
    r = policy_opa.consult(policy_opa.build_input("HIGH"), actual="ask")
    r2 = policy_opa.consult(policy_opa.build_input("HIGH"), actual="ask")
    unavailable = [e for e in audit_events() if e["event"] == "opa_unavailable"]
    check("OPA down: the gate's decision stands and nothing is loosened", r["final"] == "ask" and r["opa"] is None)
    check("OPA down: one audit note, not a flood", len(unavailable) == 1 and r2["final"] == "ask", f"{len(unavailable)} note(s)")
    check("the private audit log stayed a valid chain", audit.verify().ok)


if __name__ == "__main__":
    try:
        main()
    except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
        print(f"  FAIL  unexpected error: {e!r}")
        failures += 1
    print(f"OPA: {'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    sys.exit(1 if failures else 0)
