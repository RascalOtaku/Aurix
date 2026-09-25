"""A minimal NATS + JetStream client on the standard library only (the NATS wire protocol is plain text over TCP).

Just enough for the proof of concept (Activation Handoff §10 task 6): core publish/subscribe, request/reply, and the JetStream
API (create a stream, publish with an acknowledgement, read a message back by sequence number). Not a production client.
"""
from __future__ import annotations

import base64
import json
import socket
import uuid
from typing import Any, Dict, Optional, Tuple


class NatsError(RuntimeError):
    pass


class Nats:
    def __init__(self, host: str = "nats", port: int = 4222, timeout: float = 5.0):
        self.timeout = timeout
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.buf = b""
        self._sid = 0
        line = self._readline()
        if not line.startswith(b"INFO "):
            raise NatsError(f"expected INFO, got {line[:40]!r}")
        self.info: Dict[str, Any] = json.loads(line[5:])
        self._send(b'CONNECT {"verbose":false,"pedantic":false,"protocol":1,"lang":"python-stdlib","version":"poc","headers":false}\r\n')
        self.ping()

    # -- wire ----------------------------------------------------------------------------------------------------------------
    def _send(self, data: bytes) -> None:
        self.sock.sendall(data)

    def _fill(self) -> None:
        chunk = self.sock.recv(65536)
        if not chunk:
            raise NatsError("connection closed by the server")
        self.buf += chunk

    def _readline(self) -> bytes:
        while b"\r\n" not in self.buf:
            self._fill()
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line

    def _readn(self, n: int) -> bytes:
        while len(self.buf) < n + 2:
            self._fill()
        data, self.buf = self.buf[:n], self.buf[n + 2:]
        return data

    def ping(self) -> None:
        self._send(b"PING\r\n")
        while True:
            line = self._readline()
            if line == b"PONG":
                return
            if line == b"PING":
                self._send(b"PONG\r\n")
            elif line.startswith(b"-ERR"):
                raise NatsError(line.decode())

    # -- core ----------------------------------------------------------------------------------------------------------------
    def subscribe(self, subject: str) -> int:
        self._sid += 1
        self._send(f"SUB {subject} {self._sid}\r\n".encode())
        self.ping()                                  # the server has processed the subscription once it answers
        return self._sid

    def publish(self, subject: str, payload: bytes = b"", reply: Optional[str] = None) -> None:
        head = f"PUB {subject} {reply} {len(payload)}\r\n" if reply else f"PUB {subject} {len(payload)}\r\n"
        self._send(head.encode() + payload + b"\r\n")

    def next_message(self, timeout: Optional[float] = None) -> Tuple[str, int, bytes]:
        """(subject, sid, payload) of the next MSG, answering server PINGs on the way."""
        self.sock.settimeout(timeout or self.timeout)
        while True:
            line = self._readline()
            if line == b"PING":
                self._send(b"PONG\r\n")
            elif line.startswith(b"MSG "):
                parts = line.split()
                subject, sid, size = parts[1].decode(), int(parts[2]), int(parts[-1])
                return subject, sid, self._readn(size)
            elif line.startswith(b"-ERR"):
                raise NatsError(line.decode())

    def request(self, subject: str, payload: bytes = b"", timeout: Optional[float] = None) -> bytes:
        inbox = f"_INBOX.{uuid.uuid4().hex}"
        self.subscribe(inbox)
        self.publish(subject, payload, reply=inbox)
        return self.next_message(timeout)[2]

    # -- JetStream -----------------------------------------------------------------------------------------------------------
    def js_api(self, endpoint: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        raw = self.request("$JS.API." + endpoint, json.dumps(body).encode() if body is not None else b"")
        return json.loads(raw)

    def js_publish(self, subject: str, payload: bytes) -> Dict[str, Any]:
        """Publish and wait for the JetStream acknowledgement {'stream', 'seq'} - proof the message was persisted."""
        ack = json.loads(self.request(subject, payload))
        if "error" in ack:
            raise NatsError(f"JetStream refused the publish: {ack['error']}")
        return ack

    def js_get(self, stream: str, seq: int) -> Optional[Tuple[str, bytes]]:
        resp = self.js_api(f"STREAM.MSG.GET.{stream}", {"seq": seq})
        msg = resp.get("message")
        if not msg:
            return None
        return msg["subject"], base64.b64decode(msg["data"])

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass
