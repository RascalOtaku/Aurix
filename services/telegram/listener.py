"""services/telegram/listener.py - inbound Telegram polling listener for AURIX.

Polls Telegram getUpdates, verifies sender is TELEGRAM_CHAT_ID, and routes
text messages through the real agent loop (/api/chat_stream, mode=agent)
via the internal-tool loopback auth path - the same pattern
src/tool_implementations.py already uses for cookbook routes.

Processes up to TELEGRAM_MAX_CONCURRENT messages at once (default 1, per
tonight's testing showing this box's CPU-bound qwen2.5:3b can't usefully
serve concurrent agent rounds - see session notes). Sends an immediate ack
on receipt so the user isn't left wondering if a message was seen during a
slow agent round. Agent-loop timeout raised to 360s (from 180s) after a
real timeout was observed on a CPU-bound inference round.
"""
import asyncio
import json
import logging
import os
import re
import time
import urllib.request

import httpx

from services.telegram.service import TelegramService
from src.approval_gate import (
    APPROVAL_TIMEOUT_SECONDS, bare_reply_hint, handle_command, parse_approval_command,
)
from src.foundation import buttons as buttons_mod
from src.foundation import commands as fcommands
from src.foundation import honesty
from src.foundation import session_rotation

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
POLL_TIMEOUT = 25
POLL_INTERVAL_ERROR = 5
# The agent stream goes silent while a tool call waits on owner approval, so the
# read timeout must cover the inference budget plus the approval window.
AGENT_LOOP_TIMEOUT = int(os.environ.get("TELEGRAM_AGENT_TIMEOUT", "360")) + APPROVAL_TIMEOUT_SECONDS

ODYSSEUS_URL = os.environ.get("ODYSSEUS_INTERNAL_URL", "http://127.0.0.1:7000")
TELEGRAM_AGENT_OWNER = os.environ.get("TELEGRAM_AGENT_OWNER", "rascal")
MAX_CONCURRENT_MESSAGES = int(os.environ.get("TELEGRAM_MAX_CONCURRENT", "1"))
_CALLBACK_RE = re.compile(r"^(approve|deny):([0-9a-f]{6})$")


def _agent_session_id():
    """The chat session to use for this turn - read live, not a frozen env var, so a daily rotation
    (session_rotation.daily_rotate) takes effect immediately, no restart needed."""
    return session_rotation.current_session_id()


def _internal_headers(owner: str = None) -> dict:
    from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_TOKEN
    headers = {INTERNAL_TOOL_HEADER: INTERNAL_TOOL_TOKEN}
    if owner:
        headers["X-Odysseus-Owner"] = owner
    return headers


class TelegramListener:
    def __init__(self):
        self.bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        self.enabled = os.environ.get("TELEGRAM_ENABLED", "true").lower() == "true"
        self._service = TelegramService()
        self._offset = None
        self._task = None
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_MESSAGES)
        self._in_flight = set()
        self._control_tasks = set()
        self._foundation = None
        self._sched_task = None
        self._last_poll_ok = None     # when Telegram last ANSWERED a poll (ok=true): the deploy readiness signal

    # -- Foundation (missions) ---------------------------------------------------
    async def _notify(self, text: str, buttons: dict = None) -> None:
        """Send to the owner; `buttons` is an inline keyboard (tap instead of type)."""
        loop = asyncio.get_event_loop()
        if buttons:
            await loop.run_in_executor(None, lambda: self._service.send(text, reply_markup=buttons))
        else:
            await loop.run_in_executor(None, self._service.send, text)

    async def _run_agent_serialised(self, prompt: str) -> str:
        """Mission steps share the one agent session with chat: take turns."""
        async with self._semaphore:
            return await self._call_agent_loop(prompt)

    async def _send_file(self, path: str, caption: str = "") -> bool:
        res = await asyncio.get_event_loop().run_in_executor(None, self._service.send_document, path, caption)
        if not res.ok:
            logger.warning("Telegram send_document failed for %s: %s", os.path.basename(path), res.error)
        return res.ok

    async def _handle_audio(self, msg: dict) -> None:
        """A voice note, audio file, or audio-typed document: real transcription work, not a chat message."""
        from src.foundation import transcription
        voice = msg.get("voice") or msg.get("audio")
        doc = msg.get("document")
        if voice:
            file_id, filename, duration = voice["file_id"], voice.get("file_name") or f"{voice.get('mime_type', 'audio').split('/')[-1]}.oga", voice.get("duration")
        elif doc and str(doc.get("mime_type", "")).startswith("audio/"):
            file_id, filename, duration = doc["file_id"], doc.get("file_name") or "audio", None
        else:
            return
        loop = asyncio.get_event_loop()

        def notify_sync(text):
            self._service.send(text)

        def send_file_sync(path, caption):
            self._service.send_document(path, caption)
        ack = await loop.run_in_executor(None, transcription.request, file_id, filename, duration, notify_sync, send_file_sync)
        await loop.run_in_executor(None, self._service.send, ack)

    async def _handle_photo(self, msg: dict) -> None:
        """A photo (or image file): answered by the LOCAL vision model, the caption is the question (src/foundation/vision.py)."""
        from src.foundation import vision
        sizes = msg.get("photo") or []
        file_id = (max(sizes, key=lambda p: p.get("file_size") or p.get("width", 0)).get("file_id") if sizes
                   else (msg.get("document") or {}).get("file_id"))
        if not file_id:
            return
        loop = asyncio.get_event_loop()
        reply = await loop.run_in_executor(None, vision.photo, file_id, (msg.get("caption") or "").strip())
        await loop.run_in_executor(None, self._service.send, reply)

    def _cancel_in_flight(self) -> None:
        for task in list(self._in_flight):
            task.cancel()

    async def _notify_unprompted(self, text: str, buttons: dict = None) -> None:
        """Everything Aurix says on its own (scheduler, watchdog, missions) goes through the outbox: quiet hours, the
        hourly cap, emergencies with one follow-up (src/foundation/outbox.py). Replies to the owner use _notify."""
        from src.foundation import outbox
        if outbox.decide(text) == "send":
            await self._notify(text, buttons)

    def _foundation_obj(self):
        if self._foundation is None:
            from src.foundation.llm_bridge import default_llm
            self._foundation = fcommands.Foundation(
                run_agent=self._run_agent_serialised, notify=self._notify_unprompted, llm=default_llm,
                session_id=_agent_session_id(), on_stop=self._cancel_in_flight, send_file=self._send_file)
        return self._foundation

    async def _handle_foundation(self, kind: str, arg: str) -> None:
        try:
            if kind == "new":
                await self._notify("planning... (working out what this needs)")
            reply = await self._foundation_obj().handle(kind, arg)
            await self._notify(reply, buttons_mod.for_reply(kind, reply))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("foundation command %s failed: %r", kind, e)
            await self._notify(f"Mission command '{kind}' failed: {e!r}")

    def _api(self, method: str, payload: dict) -> dict:
        url = f"{API_BASE}/bot{self.bot_token}/{method}"
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=POLL_TIMEOUT + 10) as r:
                return json.loads(r.read())
        except Exception as e:
            logger.warning("Telegram getUpdates call failed: %s", e)
            return {}

    async def _get_updates(self):
        payload = {"timeout": POLL_TIMEOUT, "allowed_updates": ["message", "callback_query"]}
        if self._offset:
            payload["offset"] = self._offset
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, self._api, "getUpdates", payload)
        if result.get("ok") is True:                   # a real answer from Telegram, not the {} that _api returns on failure
            self._last_poll_ok = time.time()
        return result.get("result", [])

    async def _call_agent_loop(self, text: str) -> str:
        headers = _internal_headers(owner=TELEGRAM_AGENT_OWNER)
        form = {
            "message": text,
            "session": _agent_session_id(),
            "preset_id": "aurix",
            "mode": "agent",
            "allow_bash": "true",
            "allow_web_search": "true",           # without this the whole web_search tool is disabled at the request level, before the
                                                   # mission's own contract/capabilities even get a say - broke every pack that needs it
                                                   # (reynolds_research, real_estate_leads, social_content, ct_to_print's cheapest-producer
                                                   # search...) for real Telegram-dispatched missions, found live 2026-09-23
        }
        chunks = []
        tools_ran = []                                   # the ONLY proof a tool ran: the agent loop's own tool_output events
        async with httpx.AsyncClient(timeout=AGENT_LOOP_TIMEOUT) as client:
            async with client.stream("POST", f"{ODYSSEUS_URL}/api/chat_stream",
                                      data=form, headers=headers) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    payload = line[len("data: "):]
                    if payload.strip() == "[DONE]":
                        break
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if "delta" in event:
                        chunks.append(event["delta"])
                    elif event.get("type") == "tool_output":
                        tools_ran.append({k: event.get(k) for k in ("tool", "exit_code", "output")})
                    elif event.get("type") == "metrics":
                        round_texts = event.get("data", {}).get("round_texts", [])
                        non_empty = [t for t in round_texts if t.strip()]
                        if non_empty:
                            chunks = [non_empty[-1]]
        return honesty.check("".join(chunks).strip(), tools_ran)          # never a claim the tools do not back up

    async def _handle_message(self, text: str) -> str:
        if not _agent_session_id():
            return ("AURIX's Telegram agent session isn't bootstrapped - "
                     "set TELEGRAM_AGENT_SESSION_ID in .env.")
        try:
            return await self._call_agent_loop(text)
        except Exception as e:
            logger.error("Telegram agent-loop call failed: %s", repr(e))
            reason = "it took too long" if isinstance(e, httpx.TimeoutException) else type(e).__name__
            return f"❌ No answer: the agent failed ({reason}). Nothing was done."

    async def _process_message(self, text: str):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._service.send, "working on it...")
        async with self._semaphore:
            reply = await self._handle_message(text)
            await loop.run_in_executor(None, self._service.send, reply)

    async def _handle_callback(self, cq: dict) -> None:
        """Inline Approve/Deny button: exactly the authority of typing the command, i.e. only the owner's own chat."""
        loop = asyncio.get_event_loop()
        cq_id = cq.get("id")
        sender = str((cq.get("from") or {}).get("id", ""))
        chat = str((((cq.get("message") or {}).get("chat")) or {}).get("id", ""))
        if not self.chat_id or sender != str(self.chat_id) or chat != str(self.chat_id):
            logger.warning("ignored a button press from an unexpected sender/chat")
            await loop.run_in_executor(None, self._api, "answerCallbackQuery", {"callback_query_id": cq_id})
            return
        from src.foundation import outbox
        outbox.note_owner_active()                              # a tap means the owner is awake
        cmd = buttons_mod.command_from_data(cq.get("data") or "")
        if cmd is not None:
            # A tap-instead-of-type button: the same owner check as above, the same parser as typing, and only allow-listed kinds.
            control = fcommands.parse(cmd)
            if not control or control[0] not in buttons_mod.ALLOWED_KINDS:
                await loop.run_in_executor(None, self._api, "answerCallbackQuery", {"callback_query_id": cq_id, "text": "Unknown button"})
                return
            await loop.run_in_executor(None, self._api, "answerCallbackQuery", {"callback_query_id": cq_id, "text": "On it..."})
            message = cq.get("message") or {}
            if message.get("message_id"):                 # one tap only: take the buttons off the message
                await loop.run_in_executor(None, self._api, "editMessageReplyMarkup",
                                           {"chat_id": self.chat_id, "message_id": message["message_id"],
                                            "reply_markup": {"inline_keyboard": []}})
            ctask = asyncio.create_task(self._handle_foundation(*control))
            self._control_tasks.add(ctask)
            ctask.add_done_callback(self._control_tasks.discard)
            return
        m = _CALLBACK_RE.match(cq.get("data") or "")
        if not m:
            await loop.run_in_executor(None, self._api, "answerCallbackQuery",
                                       {"callback_query_id": cq_id, "text": "Unknown button"})
            return
        reply = handle_command(m.group(1), m.group(2), resolver="telegram-button")
        await loop.run_in_executor(None, self._api, "answerCallbackQuery",
                                   {"callback_query_id": cq_id, "text": reply[:190]})
        message = cq.get("message") or {}
        if message.get("message_id"):                 # remove the buttons so a second tap cannot do anything
            await loop.run_in_executor(None, self._api, "editMessageReplyMarkup",
                                       {"chat_id": self.chat_id, "message_id": message["message_id"],
                                        "reply_markup": {"inline_keyboard": []}})

    async def _loop(self):
        logger.info("Telegram listener started (agent-loop mode, %s, max_concurrent=%d, timeout=%ds)",
                    ODYSSEUS_URL, MAX_CONCURRENT_MESSAGES, AGENT_LOOP_TIMEOUT)
        while True:
            try:
                updates = await self._get_updates()
                for update in updates:
                    self._offset = update["update_id"] + 1
                    if update.get("callback_query"):
                        await self._handle_callback(update["callback_query"])
                        continue
                    msg = update.get("message", {})
                    chat = msg.get("chat", {})
                    if str(chat.get("id", "")) != str(self.chat_id):
                        continue
                    from src.foundation import outbox
                    outbox.note_owner_active()                  # the owner is awake: quiet hours step aside
                    doc = msg.get("document") or {}
                    if msg.get("voice") or msg.get("audio") or str(doc.get("mime_type", "")).startswith("audio/"):
                        atask = asyncio.create_task(self._handle_audio(msg))
                        self._control_tasks.add(atask)
                        atask.add_done_callback(self._control_tasks.discard)
                        continue
                    if msg.get("photo") or str(doc.get("mime_type", "")).startswith("image/"):
                        ptask = asyncio.create_task(self._handle_photo(msg))
                        self._control_tasks.add(ptask)
                        ptask.add_done_callback(self._control_tasks.discard)
                        continue
                    text = (msg.get("text") or "").strip()
                    if not text:
                        continue
                    logger.info("Telegram inbound: %s", text[:100])
                    # Approval replies are handled inline, never queued behind the
                    # agent semaphore: the message waiting on approval holds it.
                    approval_cmd = parse_approval_command(text)
                    if approval_cmd:
                        reply = handle_command(*approval_cmd, resolver="telegram")
                        await asyncio.get_event_loop().run_in_executor(
                            None, self._service.send, reply)
                        continue
                    # Owner control commands (missions, STOP, status, authorize). Parsed only
                    # here, after the sender is verified as the owner, never from the agent.
                    # STOP is absolute: handled inline, immediately, never queued.
                    control = fcommands.parse(text)
                    if control:
                        if control[0] == "stop":
                            await self._handle_foundation(*control)
                        else:
                            ctask = asyncio.create_task(self._handle_foundation(*control))
                            self._control_tasks.add(ctask)
                            ctask.add_done_callback(self._control_tasks.discard)
                        continue
                    try:                                    # things you say about yourself become memory PROPOSALS (never memories); cheap, silent
                        fcommands.observe_owner_text(text)
                    except Exception:
                        pass
                    # A bare "approve"/"yes"/"no" while something is pending is
                    # neither an approval nor a message for the agent.
                    hint = bare_reply_hint(text)
                    if hint:
                        await asyncio.get_event_loop().run_in_executor(
                            None, self._service.send, hint)
                        continue
                    from src.foundation import intent    # optional tiny local router: plain speech -> a SAFE command
                    if intent.enabled():
                        routed = await asyncio.get_event_loop().run_in_executor(None, intent.route, text)
                        if routed:
                            rtask = asyncio.create_task(self._handle_foundation(*routed))
                            self._control_tasks.add(rtask)
                            rtask.add_done_callback(self._control_tasks.discard)
                            continue
                    task = asyncio.create_task(self._process_message(text))
                    self._in_flight.add(task)
                    task.add_done_callback(self._in_flight.discard)
            except asyncio.CancelledError:
                logger.info("Telegram listener stopping")
                break
            except Exception as e:
                logger.error("Telegram listener loop error: %s", e)
                await asyncio.sleep(POLL_INTERVAL_ERROR)

    async def _standing_loop(self):
        """Once a minute: run any standing (scheduled) missions that are due, and the opt-in digest."""
        logger.info("Standing-mission scheduler started")
        try:
            await self._foundation_obj().startup_notice()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("startup notice failed: %r", e)
        while True:
            try:
                await self._foundation_obj().tick_standing()
                from src.foundation import outbox
                for text in outbox.due():                        # the morning summary, or one emergency follow-up
                    await self._notify(text)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("standing scheduler pass failed: %r", e)
            await asyncio.sleep(60)

    def start(self):
        if not self.enabled or not self.bot_token or not self.chat_id:
            logger.warning("Telegram listener not started - missing token/chat_id or disabled")
            return
        if self._task is None:
            self._task = asyncio.create_task(self._loop())
            logger.info("Telegram listener task created")
        if self._sched_task is None and os.environ.get("AURIX_STANDING_ENABLED", "1") != "0":
            self._sched_task = asyncio.create_task(self._standing_loop())

    def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None
        if self._sched_task:
            self._sched_task.cancel()
            self._sched_task = None


_listener_singleton = None


def start_telegram_listener():
    global _listener_singleton
    if _listener_singleton is None:
        _listener_singleton = TelegramListener()
    _listener_singleton.start()
    return _listener_singleton
