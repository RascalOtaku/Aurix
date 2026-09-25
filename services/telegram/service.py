# services/telegram/service.py
"""Telegram notification service — send alerts and reports to the owner's Telegram."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import html
import json
import logging
import os
import re
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)


@dataclass
class TelegramResult:
    """Result of a Telegram send attempt."""
    ok: bool
    error: str = ""
    fell_back_to_file: bool = False


class TelegramService:
    """
    Telegram notification service. Absorbed from the legacy core/telegram.py
    script — same behavior (send + fallback to file on failure), but as a
    proper odysseus service so routes/tasks can depend on it via DI instead
    of a bare module-level import.

    Usage:
        service = TelegramService()
        result = service.send_alert("Something happened")
    """

    API_BASE = "https://api.telegram.org"
    MAX_MESSAGE_LEN = 4096

    def __init__(
        self,
        bot_token: str | None = None,
        chat_id: str | None = None,
        fallback_dir: Path | None = None,
        timeout: int = 15,
    ):
        self.bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
        self.enabled = os.environ.get("TELEGRAM_ENABLED", "true").lower() == "true"
        # Use the app's own writable data dir rather than a hardcoded
        # personal home path (the bug we fixed in cortex/resource_governor.py).
        self.fallback_dir = fallback_dir or Path(
            os.getenv("AURIX_PROJECT_ROOT", "/app")
        ) / "data" / "unsent_reports"
        self.timeout = timeout

    def _fallback(self, reason: str, text: str) -> None:
        try:
            self.fallback_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            path = self.fallback_dir / f"{ts}.txt"
            path.write_text(f"[{reason}]\n{text}", encoding="utf-8")
            logger.warning("Telegram send failed (%s); wrote fallback to %s", reason, path)
        except Exception as e:
            logger.error("Telegram fallback write also failed: %s", e)

    CHUNK_LEN = 3900        # under the 4096 limit, leaving room for the "(2/3)" markers

    @classmethod
    def split_message(cls, text: str) -> list:
        """Split on line boundaries into pieces Telegram will accept (a long line is hard-split)."""
        text = text or ""
        if len(text) <= cls.MAX_MESSAGE_LEN:
            return [text]
        chunks, cur = [], ""
        for line in text.split("\n"):
            while len(line) > cls.CHUNK_LEN:                    # a single huge line
                if cur:
                    chunks.append(cur)
                    cur = ""
                chunks.append(line[:cls.CHUNK_LEN])
                line = line[cls.CHUNK_LEN:]
            if len(cur) + len(line) + 1 > cls.CHUNK_LEN and cur:
                chunks.append(cur)
                cur = ""
            cur = f"{cur}\n{line}" if cur else line
        if cur:
            chunks.append(cur)
        return chunks

    _TG_TAG = re.compile(r"</?(?:b|strong|i|em|u|ins|s|strike|del|code|pre|a|tg-spoiler|blockquote)(?:\s[^<>]*)?>", re.I)

    @classmethod
    def strip_html(cls, text: str) -> str:
        """Plain-text version: removes only Telegram's own tags, so a literal 'a < b' survives."""
        return html.unescape(cls._TG_TAG.sub("", text or ""))

    def _post_message(self, text: str, parse_mode: str | None, reply_markup: dict | None = None) -> tuple:
        """One sendMessage call -> (ok, error). Never raises."""
        body = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}
        if parse_mode:
            body["parse_mode"] = parse_mode
        if reply_markup:
            body["reply_markup"] = reply_markup
        req = urllib.request.Request(
            f"{self.API_BASE}/bot{self.bot_token}/sendMessage", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                resp = json.loads(r.read())
            return (True, "") if resp.get("ok") else (False, str(resp)[:300])
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read()).get("description", "")
            except Exception:
                detail = ""
            return False, f"HTTP {e.code}: {detail}"[:300]
        except Exception as e:
            return False, str(e)[:300]

    def send(self, text: str, parse_mode: str = "HTML", reply_markup: dict | None = None) -> TelegramResult:
        """Send a message. Long text is split into several messages; if Telegram rejects the markup
        (agent replies routinely contain a stray '<'), the same text is re-sent as plain text instead of
        being lost. Only if that also fails does it fall back to a file."""
        if not self.enabled:
            return TelegramResult(ok=False, error="disabled")
        if not self.bot_token or not self.chat_id:
            self._fallback("no_token", text)
            return TelegramResult(ok=False, error="no_token", fell_back_to_file=True)

        chunks = self.split_message(text)
        for i, chunk in enumerate(chunks, 1):
            if len(chunks) > 1:
                chunk = f"({i}/{len(chunks)})\n{chunk}"
            markup = reply_markup if i == len(chunks) else None          # buttons ride on the last piece
            ok, err = self._post_message(chunk, parse_mode, markup)
            if not ok and parse_mode and "parse" in err.lower():
                ok, err = self._post_message(self.strip_html(chunk)[: self.MAX_MESSAGE_LEN], None, markup)
            if not ok:
                self._fallback(err, text)
                return TelegramResult(ok=False, error=err, fell_back_to_file=True)
        return TelegramResult(ok=True)

    MAX_DOCUMENT_BYTES = 45 * 1024 * 1024        # Bot API limit is 50 MB; stay clear of it

    def send_document(self, path: str | Path, caption: str = "") -> TelegramResult:
        """Upload a file to the owner's chat (multipart/form-data, stdlib only)."""
        if not self.enabled:
            return TelegramResult(ok=False, error="disabled")
        if not self.bot_token or not self.chat_id:
            return TelegramResult(ok=False, error="no_token")
        p = Path(path)
        try:
            size = p.stat().st_size
            if size > self.MAX_DOCUMENT_BYTES:
                return TelegramResult(ok=False, error=f"file too large ({size // (1024 * 1024)} MB)")
            blob = p.read_bytes()
        except OSError as e:
            return TelegramResult(ok=False, error=f"unreadable: {e}")
        boundary = "----aurix" + os.urandom(8).hex()
        parts = []
        for name, value in (("chat_id", self.chat_id), ("caption", caption[:1000])):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in p.name) or "file"
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{safe_name}"\r\n'
                      "Content-Type: application/octet-stream\r\n\r\n").encode() + blob + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        req = urllib.request.Request(
            f"{self.API_BASE}/bot{self.bot_token}/sendDocument", data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=max(self.timeout, 60)) as r:
                resp = json.loads(r.read())
            return TelegramResult(ok=bool(resp.get("ok")), error="" if resp.get("ok") else str(resp)[:200])
        except Exception as e:
            return TelegramResult(ok=False, error=str(e)[:200])

    def send_report(self, subject: str, body: str) -> TelegramResult:
        """Send a formatted AURIX report."""
        ts = datetime.now().strftime("%a %b %d %H:%M")
        msg = f"<b>AURIX · {subject}</b>\n<i>{ts}</i>\n\n{body}"
        return self.send(msg)

    def send_alert(self, text: str) -> TelegramResult:
        """Send a short alert."""
        return self.send(f"⚠️ {text}")
