"""Spec §10 task 6: "NATS pub/sub proof of concept - confirm the event bus actually carries a message from one component to another."

Phase 1 (default): (a) a publisher component sends an audit-style event, a separate subscriber component receives it;
                   (b) the same events go through JetStream and are acknowledged (persisted) with sequence numbers.
Phase 2 (--phase2 <marker>): run AFTER the NATS server was restarted: the persisted events are still there.
"""
import json
import sys
import time

from nats_min import Nats, NatsError

STREAM = "AURIX_AUDIT"
MARK = "aurix-poc-durability-marker"
failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    failures += 0 if ok else 1


def phase1():
    print("== NATS phase 1: two components, one bus")
    subscriber, publisher = Nats(), Nats()
    check("both components connect and the server identifies itself", subscriber.info.get("server_name") is not None or "version" in subscriber.info,
          f"nats-server {subscriber.info.get('version')}, JetStream={'yes' if subscriber.info.get('jetstream') else 'NO'}")
    check("JetStream is enabled", bool(subscriber.info.get("jetstream")))
    subscriber.subscribe("aurix.audit.>")
    event = {"event": "skill_forged", "name": "poc_skill", "seq": 41, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
    publisher.publish("aurix.audit.skill_forged", json.dumps(event).encode())
    subject, _sid, payload = subscriber.next_message(5)
    got = json.loads(payload)
    check("the subscriber RECEIVES what the publisher sent (core pub/sub)", subject == "aurix.audit.skill_forged" and got == event, subject)
    publisher.publish("aurix.other.topic", b"not for the subscriber")
    publisher.publish("aurix.audit.mission_completed", b'{"event": "mission_completed"}')
    subject2, _, _ = subscriber.next_message(5)
    check("subject filtering works (an unrelated subject is not delivered)", subject2 == "aurix.audit.mission_completed", subject2)

    resp = publisher.js_api(f"STREAM.CREATE.{STREAM}", {"name": STREAM, "subjects": ["aurix.audit.>"], "storage": "file",
                                                        "retention": "limits", "max_msgs": 100000, "num_replicas": 1})
    ok = "config" in resp or (resp.get("error", {}).get("err_code") == 10058)
    check("a persistent JetStream stream exists for the audit events", ok, str(resp.get("error") or "created"))
    acks = []
    for i in range(3):
        payload = json.dumps({"event": "poc_event", "n": i, "marker": MARK if i == 1 else "x"}).encode()
        acks.append(publisher.js_publish("aurix.audit.poc_event", payload))
    check("every JetStream publish is ACKNOWLEDGED with a sequence number (= persisted)", all(a.get("stream") == STREAM and a.get("seq") for a in acks),
          f"seqs {[a['seq'] for a in acks]}")
    check("sequence numbers strictly increase", [a["seq"] for a in acks] == sorted(a["seq"] for a in acks) and len({a["seq"] for a in acks}) == 3)
    stored = publisher.js_get(STREAM, acks[1]["seq"])
    check("a persisted message can be read back by sequence number", stored is not None and json.loads(stored[1]).get("marker") == MARK)
    info = publisher.js_api(f"STREAM.INFO.{STREAM}")
    check("the stream reports the stored messages", info.get("state", {}).get("messages", 0) >= 3, str(info.get("state", {}).get("messages")))
    print("MARKER_SEQ=%d" % acks[1]["seq"])
    subscriber.close()
    publisher.close()


def phase2(marker_seq):
    print("== NATS phase 2: after the server was restarted")
    c = Nats()
    stored = c.js_get(STREAM, marker_seq)
    check("the message persisted before the restart is STILL THERE", stored is not None and json.loads(stored[1]).get("marker") == MARK,
          f"seq {marker_seq}")
    info = c.js_api(f"STREAM.INFO.{STREAM}")
    check("the stream survived with its messages", info.get("state", {}).get("messages", 0) >= 3, str(info.get("state", {}).get("messages")))
    ack = c.js_publish("aurix.audit.poc_event", b'{"event": "after_restart"}')
    check("and it accepts new events, continuing the sequence", ack.get("seq", 0) > marker_seq, f"seq {ack.get('seq')}")
    c.close()


if __name__ == "__main__":
    try:
        if "--phase2" in sys.argv:
            phase2(int(sys.argv[sys.argv.index("--phase2") + 1]))
        else:
            phase1()
    except (NatsError, OSError, ValueError) as e:
        print(f"  FAIL  unexpected error: {e!r}")
        failures += 1
    print(f"NATS: {'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    sys.exit(1 if failures else 0)
