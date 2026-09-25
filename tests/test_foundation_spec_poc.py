"""Spec proofs of concept (Activation Handoff §10 tasks #4-#7), the parts that live in the codebase and run without Docker:
the OPA policy client + its shadow hook in the real approval gate, the audit-head witness logic, and the stdlib NATS client."""
import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import http.server
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "poc"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from src import approval_gate as ag  # noqa: E402
from src.foundation import audit, policy_opa  # noqa: E402
import audit_witness  # noqa: E402
import nats_min  # noqa: E402

TG_SESSION = "tg-session-id"


# ---------------------------------------------------------------------------------------------------------------------------
# a fake OPA server
# ---------------------------------------------------------------------------------------------------------------------------
class FakeOpa:
    """Serves /v1/data/aurix/approval/decision with a canned result (or a canned failure)."""

    def __init__(self):
        self.result = {"result": {"action": "ask", "reason": "canned"}}
        self.status = 200
        self.raw = None
        self.delay = 0.0
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
                outer.requests.append((self.path, body))
                if outer.delay:
                    time.sleep(outer.delay)
                data = outer.raw if outer.raw is not None else json.dumps(outer.result).encode()
                self.send_response(outer.status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class _Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name})
        self.env.start()
        os.environ.pop("AURIX_OPA_MODE", None)
        os.environ.pop("OPA_URL", None)
        audit._heads.clear()
        policy_opa._last_unavailable_note["at"] = 0.0

    def tearDown(self):
        audit._heads.clear()
        self.env.stop()
        self.tmp.cleanup()

    def events(self, name=None):
        try:
            with open(audit.audit_path(), encoding="utf-8") as f:
                rows = [json.loads(line) for line in f if line.strip()]
        except OSError:
            rows = []
        return [r for r in rows if name is None or r["event"] == name]


class PolicyReferenceTests(_Env):
    def test_mode_defaults_to_off_and_rejects_junk(self):
        self.assertEqual(policy_opa.mode(), "off")
        for value, want in (("shadow", "shadow"), ("SHADOW", "shadow"), (" enforce ", "enforce"), ("off", "off"),
                            ("on", "off"), ("true", "off"), ("", "off")):
            with mock.patch.dict(os.environ, {"AURIX_OPA_MODE": value}):
                self.assertEqual(policy_opa.mode(), want, value)

    def test_truth_table_matches_the_gate(self):
        c = policy_opa.code_decision
        b = policy_opa.build_input
        for tier in ("LOW", "MEDIUM", "HIGH", "CRITICAL"):
            for covers in (True, False):
                self.assertEqual(c(b(tier, True, covers)), "deny", "STOP denies everything")
        self.assertEqual(c(b("CRITICAL", False, True)), "deny")
        self.assertEqual(c(b("LOW", False, False)), "allow")
        self.assertEqual(c(b("MEDIUM", False, True)), "allow")
        self.assertEqual(c(b("MEDIUM", False, False)), "ask")
        self.assertEqual(c(b("HIGH", False, True)), "ask")             # a contract never covers HIGH

    def test_malformed_input_never_allows_and_only_a_real_bool_counts(self):
        c = policy_opa.code_decision
        for inp in ({}, {"tier": "LOW"}, {"stop": "false", "tier": "LOW"}, {"stop": None, "tier": "LOW"}, {"stop": 0, "tier": "LOW"},
                    {"stop": False, "tier": "low"}, {"stop": False, "tier": "SUPERHIGH"}, {"stop": False, "tier": None},
                    {"stop": False, "tier": "MEDIUM", "contract_covers": "true"}, {"stop": False, "tier": "MEDIUM", "contract_covers": 1}):
            self.assertNotEqual(c(inp), "allow", inp)
        self.assertEqual(c({"stop": "true", "tier": "CRITICAL"}), "deny")     # CRITICAL is denied regardless of how STOP is typed
        self.assertEqual(c({"stop": True}), "deny")

    def test_build_input_normalises(self):
        self.assertEqual(policy_opa.build_input("low"), {"stop": False, "tier": "LOW", "contract_covers": False})
        self.assertEqual(policy_opa.build_input("HIGH", 1, "yes"), {"stop": True, "tier": "HIGH", "contract_covers": True})

    def test_stricter(self):
        s = policy_opa.stricter
        self.assertEqual((s("allow", "ask"), s("ask", "allow"), s("ask", "deny"), s("deny", "allow"), s("allow", "allow")),
                         ("ask", "ask", "deny", "deny", "allow"))
        self.assertEqual(s("allow", "nonsense"), "ask")                    # an unrecognised answer is treated as 'ask', never as 'allow'

    def test_the_rego_file_states_the_same_rules(self):
        rego = open(os.path.join(ROOT, "poc", "policies", "aurix.rego"), encoding="utf-8").read()
        self.assertIn("package aurix.approval", rego)
        self.assertIn('default decision := {"action": "ask"', rego)         # fail closed
        for needle in ('input.stop == true', 'input.tier == "CRITICAL"', 'input.tier == "LOW"', "input.contract_covers == true"):
            self.assertIn(needle, rego)
        self.assertNotIn("action\": \"allow\", \"reason\": \"anything", rego)


class QueryTests(_Env):
    def setUp(self):
        super().setUp()
        self.opa = FakeOpa()
        self.addCleanup(self.opa.close)

    def test_a_good_answer_is_parsed_and_the_input_is_posted(self):
        self.opa.result = {"result": {"action": "allow", "reason": "low risk"}}
        got = policy_opa.query({"stop": False, "tier": "LOW", "contract_covers": False}, self.opa.url)
        self.assertEqual(got, {"action": "allow", "reason": "low risk"})
        path, body = self.opa.requests[0]
        self.assertEqual(path, "/v1/data/aurix/approval/decision")
        self.assertEqual(body, {"input": {"stop": False, "tier": "LOW", "contract_covers": False}})

    def test_every_kind_of_failure_reads_as_none(self):
        inp = policy_opa.build_input("LOW")
        self.opa.status = 500
        self.assertIsNone(policy_opa.query(inp, self.opa.url))
        self.opa.status = 200
        for raw in (b"not json", b"[]", b'{"result": "allow"}', b'{"result": {"action": "maybe"}}', b'{"result": {}}', b"{}", b'{"result": null}'):
            self.opa.raw = raw
            self.assertIsNone(policy_opa.query(inp, self.opa.url), raw)
        self.opa.raw = None
        self.assertIsNone(policy_opa.query(inp, "http://127.0.0.1:1"))       # nothing listening
        self.opa.delay = 1.0
        t = time.time()
        self.assertIsNone(policy_opa.query(inp, self.opa.url, timeout=0.2))  # too slow
        self.assertLess(time.time() - t, 0.9)

    def test_off_never_contacts_opa(self):
        os.environ["OPA_URL"] = self.opa.url
        r = policy_opa.consult(policy_opa.build_input("HIGH"), "send_email", actual="allow")
        self.assertEqual((r["mode"], r["opa"], r["final"]), ("off", None, "allow"))
        self.assertEqual(self.opa.requests, [])
        policy_opa.shadow("HIGH", False, False, "x", "allow")
        self.assertEqual(self.opa.requests, [])

    def test_shadow_records_disagreements_but_never_changes_the_outcome(self):
        os.environ.update(AURIX_OPA_MODE="shadow", OPA_URL=self.opa.url)
        self.opa.result = {"result": {"action": "ask", "reason": "needs the owner's approval"}}
        r = policy_opa.consult(policy_opa.build_input("HIGH"), "send_email", actual="allow")     # the gate (wrongly) allowed a HIGH
        self.assertEqual((r["code"], r["opa"], r["final"]), ("allow", "ask", "allow"))           # outcome untouched
        ev = self.events("opa_disagreement")
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["tool"], ev[0]["tier"], ev[0]["code"], ev[0]["opa"]), ("send_email", "HIGH", "allow", "ask"))
        r = policy_opa.consult(policy_opa.build_input("HIGH"), "send_email", actual="ask")       # agreement is silent
        self.assertEqual(len(self.events("opa_disagreement")), 1)
        self.assertTrue(audit.verify().ok)

    def test_actual_beats_the_recomputation(self):
        os.environ.update(AURIX_OPA_MODE="shadow", OPA_URL=self.opa.url)
        self.opa.result = {"result": {"action": "allow", "reason": "low risk"}}
        # the gate has a special case: a LOW bash command still asks when APPROVAL_AUTO_ALLOW_READONLY=0
        r = policy_opa.consult(policy_opa.build_input("LOW"), "bash", actual="ask")
        self.assertEqual((r["code"], r["opa"], r["final"]), ("ask", "allow", "ask"))
        self.assertEqual(len(self.events("opa_disagreement")), 1)

    def test_enforce_takes_the_stricter_answer_and_never_loosens(self):
        os.environ.update(AURIX_OPA_MODE="enforce", OPA_URL=self.opa.url)
        for opa_says in ("allow", "ask", "deny"):
            self.opa.result = {"result": {"action": opa_says, "reason": "x"}}
            for actual in ("allow", "ask", "deny"):
                r = policy_opa.consult(policy_opa.build_input("MEDIUM"), actual=actual)
                self.assertGreaterEqual(policy_opa._STRICTNESS[r["final"]], policy_opa._STRICTNESS[actual], (opa_says, actual))
                self.assertEqual(r["final"], policy_opa.stricter(actual, opa_says))

    def test_opa_down_leaves_the_decision_alone_with_one_note_not_a_flood(self):
        os.environ.update(AURIX_OPA_MODE="enforce", OPA_URL="http://127.0.0.1:1")
        for _ in range(5):
            r = policy_opa.consult(policy_opa.build_input("HIGH"), actual="ask")
            self.assertEqual((r["opa"], r["final"]), (None, "ask"))
        self.assertEqual(len(self.events("opa_unavailable")), 1)
        policy_opa._last_unavailable_note["at"] -= 700                                          # 10 minutes later: one more note
        policy_opa.consult(policy_opa.build_input("HIGH"), actual="ask")
        self.assertEqual(len(self.events("opa_unavailable")), 2)

    def test_shadow_never_raises(self):
        os.environ.update(AURIX_OPA_MODE="shadow", OPA_URL=self.opa.url)
        with mock.patch.object(policy_opa, "consult", side_effect=RuntimeError("boom")):
            self.assertIsNone(policy_opa.shadow("HIGH", False, False, "x", "ask"))


# ---------------------------------------------------------------------------------------------------------------------------
# the hook in the REAL approval gate
# ---------------------------------------------------------------------------------------------------------------------------
class GateHookTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"TELEGRAM_AGENT_SESSION_ID": TG_SESSION, "AURIX_PROJECT_ROOT": self.tmp.name})
        self.env.start()
        os.environ.pop("APPROVAL_GATE_MODE", None)
        os.environ.pop("AURIX_OPA_MODE", None)
        ag.reset_state()
        audit._heads.clear()
        self.sent = []

        async def notifier(text):
            self.sent.append(text)
            return True
        ag.set_notifier(notifier)
        self.calls = []

    def tearDown(self):
        ag.set_notifier(None)
        ag.reset_state()
        audit._heads.clear()
        self.env.stop()
        self.tmp.cleanup()

    def record(self, *args, **kw):
        self.calls.append(args)

    async def settle(self, n=1):
        for _ in range(100):
            if len(self.calls) >= n:
                return
            await asyncio.sleep(0.02)

    async def test_off_by_default_opa_is_never_touched(self):
        with mock.patch.object(policy_opa, "shadow", side_effect=self.record):
            self.assertIsNone(await ag.enforce("bash", "df -h", TG_SESSION))
            await asyncio.sleep(0.1)
        self.assertEqual(self.calls, [])

    async def test_shadow_sees_the_real_decisions_with_the_real_tier_and_outcome(self):
        os.environ["AURIX_OPA_MODE"] = "shadow"
        with mock.patch.object(policy_opa, "shadow", side_effect=self.record):
            self.assertIsNone(await ag.enforce("bash", "df -h", TG_SESSION))                  # LOW read-only -> allow
            await self.settle(1)
            self.assertEqual(self.calls[-1], ("LOW", False, False, "bash", "allow"))
            denied = await ag.enforce("write_file", "src/foundation/audit.py\nx", TG_SESSION)  # protected component -> CRITICAL -> deny
            self.assertIn("Denied", denied)
            await self.settle(2)
            self.assertEqual(self.calls[-1][0], "CRITICAL")
            self.assertEqual(self.calls[-1][4], "deny")
            task = asyncio.create_task(ag.enforce("send_email", "to: a@b.c\nhi", TG_SESSION))   # HIGH -> ask the owner
            for _ in range(200):
                if ag.pending_ids():
                    break
                await asyncio.sleep(0)
            await self.settle(3)
            self.assertEqual(self.calls[-1], ("HIGH", False, False, "send_email", "ask"))
            ag.handle_command("deny", ag.pending_ids()[0])
            self.assertIn("Denied", await task)

    async def test_the_hook_can_never_change_or_delay_the_outcome(self):
        os.environ["AURIX_OPA_MODE"] = "shadow"

        def slow_and_broken(*a, **k):
            time.sleep(1.0)
            raise RuntimeError("OPA exploded")
        with mock.patch.object(policy_opa, "shadow", side_effect=slow_and_broken):
            t = time.time()
            result = await ag.enforce("bash", "df -h", TG_SESSION)
            elapsed = time.time() - t
        self.assertIsNone(result)                                                              # same answer as without the hook
        self.assertLess(elapsed, 0.6, "the gate must not wait for OPA")

    async def test_a_hook_import_failure_is_swallowed(self):
        os.environ["AURIX_OPA_MODE"] = "shadow"
        with mock.patch.dict(sys.modules, {"src.foundation.policy_opa": None}):
            self.assertIsNone(await ag.enforce("bash", "df -h", TG_SESSION))

    async def test_ungated_sessions_are_not_shadowed(self):
        os.environ["AURIX_OPA_MODE"] = "shadow"
        with mock.patch.object(policy_opa, "shadow", side_effect=self.record):
            self.assertIsNone(await ag.enforce("bash", "ls", "some-web-session"))
            await asyncio.sleep(0.1)
        self.assertEqual(self.calls, [])


# ---------------------------------------------------------------------------------------------------------------------------
# the audit-head witness (immudb) - logic tested with a fake client; the real immudb is exercised by poc/immudb_poc.py
# ---------------------------------------------------------------------------------------------------------------------------
class FakeImmudb:
    """Stores values like immudb: append-only versions per key, verified reads."""

    class R:
        def __init__(self, value, verified=True):
            self.value, self.verified = value, verified

    def __init__(self):
        self.versions = {}

    def verifiedSet(self, key, value):
        self.versions.setdefault(key, []).append(value)
        return self.R(value)

    def verifiedGet(self, key):
        if key not in self.versions:
            raise KeyError("key not found")
        return self.R(self.versions[key][-1])


class WitnessTests(_Env):
    def make_log(self, n):
        for i in range(n):
            audit.append("event", i=i)
        return str(audit.audit_path())

    def test_head_and_hash_at(self):
        path = self.make_log(5)
        seq, h = audit_witness.audit_head(path)
        self.assertEqual(seq, 5)
        self.assertEqual(len(h), 64)
        self.assertEqual(audit_witness.hash_at(path, 5), h)
        self.assertIsNone(audit_witness.hash_at(path, 99))
        self.assertEqual(audit_witness.audit_head(os.path.join(self.tmp.name, "missing.jsonl")), (0, ""))

    def test_publish_then_check_ok_and_growth(self):
        path = self.make_log(4)
        client = FakeImmudb()
        pub = audit_witness.publish(client, path)
        self.assertEqual((pub["seq"], pub["verified"]), (4, True))
        self.assertIn(b"audit-head:4", client.versions)                        # a per-seq record, so history is never lost
        self.assertEqual(audit_witness.check(client, path)["status"], "ok")
        audit.append("more")
        audit.append("more")
        res = audit_witness.check(client, path)
        self.assertEqual((res["status"], res["grown_by"]), ("ok", 2))

    def test_tail_truncation_is_caught_even_though_the_chain_still_verifies(self):
        path = self.make_log(6)
        client = FakeImmudb()
        audit_witness.publish(client, path)
        lines = open(path, encoding="utf-8").read().splitlines()
        open(path, "w", encoding="utf-8").write("\n".join(lines[:-2]) + "\n")
        self.assertTrue(audit.verify().ok, "the file alone cannot see the truncation")
        res = audit_witness.check(client, path)
        self.assertEqual(res["status"], "tail_truncated")
        self.assertIn("seq 4", res["detail"])

    def test_a_consistent_rewrite_is_caught_even_though_the_chain_still_verifies(self):
        path = self.make_log(6)
        client = FakeImmudb()
        audit_witness.publish(client, path)
        recs = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
        prev = audit.GENESIS
        with open(path, "w", encoding="utf-8") as f:
            for r in recs:
                r.pop("hash")
                if r["seq"] == 3:
                    r["event"] = "history_was_falsified"
                r["prev"] = prev
                r["hash"] = audit._digest(prev, r)
                prev = r["hash"]
                f.write(audit._canon(r) + "\n")
        self.assertTrue(audit.verify().ok, "a consistently rewritten chain verifies")
        self.assertEqual(audit_witness.check(client, path)["status"], "rewritten")

    def test_no_witness_and_empty_logs(self):
        path = self.make_log(2)
        self.assertEqual(audit_witness.check(FakeImmudb(), path)["status"], "no_witness")
        with self.assertRaises(ValueError):
            audit_witness.publish(FakeImmudb(), os.path.join(self.tmp.name, "nothing.jsonl"))

    def test_value_extraction_across_client_versions(self):
        f = audit_witness._value_of
        self.assertEqual(f(FakeImmudb.R(b"1:a")), b"1:a")
        self.assertEqual(f(b"2:b"), b"2:b")
        self.assertEqual(f({"value": b"3:c"}), b"3:c")
        with self.assertRaises(ValueError):
            f(object())


# ---------------------------------------------------------------------------------------------------------------------------
# the stdlib NATS client against a fake server that splits frames the way a real network does
# ---------------------------------------------------------------------------------------------------------------------------
class FakeNatsServer:
    def __init__(self, split=True):
        self.split = split
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.subs = []                     # (conn, subject, sid)
        self.lock = threading.Lock()
        threading.Thread(target=self.accept, daemon=True).start()

    def accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.serve, args=(conn,), daemon=True).start()

    def matches(self, pattern, subject):
        if pattern.endswith(">"):
            return subject.startswith(pattern[:-1])
        return pattern == subject

    def send(self, conn, data):
        if self.split and len(data) > 6:                    # frame boundaries never line up with recv() calls
            conn.sendall(data[:5]); time.sleep(0.01); conn.sendall(data[5:])
        else:
            conn.sendall(data)

    def serve(self, conn):
        conn.sendall(b'INFO {"server_id":"fake","version":"0.0.1","jetstream":true,"max_payload":1048576}\r\n')
        buf = b""
        try:
            while True:
                while b"\r\n" not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    buf += chunk
                line, buf = buf.split(b"\r\n", 1)
                if line.startswith(b"PING"):
                    conn.sendall(b"PONG\r\n")
                elif line.startswith(b"SUB "):
                    _, subj, sid = line.decode().split()
                    with self.lock:
                        self.subs.append((conn, subj, sid))
                elif line.startswith(b"PUB "):
                    parts = line.decode().split()
                    subject, size = parts[1], int(parts[-1])
                    reply = parts[2] if len(parts) == 4 else None
                    while len(buf) < size + 2:
                        buf += conn.recv(4096)
                    payload, buf = buf[:size], buf[size + 2:]
                    if subject == "svc.echo" and reply:                   # a tiny request/reply service
                        self.deliver(reply, payload.upper())
                    else:
                        self.deliver(subject, payload)
                        conn.sendall(b"PING\r\n") if subject == "trigger.ping" else None
        except OSError:
            pass

    def deliver(self, subject, payload):
        with self.lock:
            targets = [(c, s) for c, pat, s in self.subs if self.matches(pat, subject)]
        for c, sid in targets:
            self.send(c, f"MSG {subject} {sid} {len(payload)}\r\n".encode() + payload + b"\r\n")

    def close(self):
        self.sock.close()


class NatsClientTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeNatsServer()
        self.addCleanup(self.server.close)
        self.clients = []

    def client(self):
        c = nats_min.Nats("127.0.0.1", self.server.port, timeout=3)
        self.clients.append(c)
        self.addCleanup(c.close)
        return c

    def test_connect_reads_info_and_completes_the_handshake(self):
        c = self.client()
        self.assertEqual(c.info["version"], "0.0.1")
        self.assertTrue(c.info["jetstream"])

    def test_publish_reaches_a_different_subscriber_with_wildcards_and_split_frames(self):
        sub, pub = self.client(), self.client()
        sub.subscribe("aurix.audit.>")
        big = json.dumps({"event": "skill_forged", "blob": "x" * 5000}).encode()               # larger than one recv
        pub.publish("aurix.audit.skill_forged", big)
        subject, sid, payload = sub.next_message(3)
        self.assertEqual((subject, payload), ("aurix.audit.skill_forged", big))
        pub.publish("other.subject", b"nope")
        pub.publish("aurix.audit.two", b"2")
        self.assertEqual(sub.next_message(3)[0], "aurix.audit.two")                              # the unrelated subject was filtered

    def test_request_reply(self):
        c = self.client()
        self.assertEqual(c.request("svc.echo", b"hello"), b"HELLO")

    def test_server_pings_are_answered_transparently(self):
        c = self.client()
        c.subscribe("trigger.ping")
        c.publish("trigger.ping", b"x")            # the fake server sends PING right after delivering
        self.assertEqual(c.next_message(3)[2], b"x")
        c.ping()                                    # still in sync

    def test_a_closed_connection_raises_instead_of_hanging(self):
        c = self.client()
        c.sock.shutdown(socket.SHUT_RDWR)
        with self.assertRaises((nats_min.NatsError, OSError)):
            c.ping()

    def test_no_server_is_a_clear_error(self):
        with self.assertRaises(OSError):
            nats_min.Nats("127.0.0.1", 1, timeout=0.5)

    def test_next_message_times_out(self):
        c = self.client()
        c.subscribe("quiet")
        with self.assertRaises((socket.timeout, TimeoutError, OSError)):
            c.next_message(0.2)


class ComposeSafetyTests(unittest.TestCase):
    """The isolation promises the compose file makes, checked from its text (no Docker needed)."""

    def setUp(self):
        self.text = open(os.path.join(ROOT, "poc", "docker-compose.spec-poc.yml"), encoding="utf-8").read()

    def test_nothing_is_published_on_the_host(self):
        for line in self.text.splitlines():
            code = line.split("#")[0]
            self.assertNotIn("ports:", code)

    def test_secrets_come_from_the_environment_never_the_file(self):
        self.assertIn("${IMMUDB_ADMIN_PASSWORD:?", self.text)
        self.assertIn("${LITELLM_MASTER_KEY:?", self.text)
        for bad in ("IMMUDB_ADMIN_PASSWORD: immudb", "password: immudb", "sk-1234"):
            self.assertNotIn(bad, self.text)

    def test_own_network_memory_caps_and_read_only_mounts(self):
        self.assertIn("name: aurix_poc_net", self.text)
        self.assertGreaterEqual(self.text.count("mem_limit:"), 5)
        self.assertGreaterEqual(self.text.count("no-new-privileges"), 4)
        self.assertIn("../src:/app/src:ro", self.text)
        self.assertIn(":/audit/audit.jsonl:ro", self.text)                                   # the real audit log is mounted READ-ONLY
        self.assertIn(":/audit/audit_ack.json:ro", self.text)
        self.assertIn("AURIX_PROJECT_ROOT: /tmp/aurix", self.text)                           # tests write their own private log
        self.assertNotIn("/var/run/docker.sock", self.text)
        self.assertNotIn("network_mode: host", self.text)

    def test_the_runner_keeps_secrets_out_of_the_repo_and_locks_the_file(self):
        sh = open(os.path.join(ROOT, "poc", "run_spec_poc.sh"), encoding="utf-8").read()
        self.assertIn("$HOME/.aurix-poc.env", sh)
        self.assertIn("chmod 600", sh)
        self.assertIn("umask 077", sh)
        self.assertNotIn("echo \"$IMMUDB", sh)


if __name__ == "__main__":
    unittest.main()
