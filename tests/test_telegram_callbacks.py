"""Inline Approve/Deny buttons in the Telegram listener: only the owner's own chat may press them, the button
carries no authority beyond typing the command, and a used/foreign/malformed press does nothing."""
import asyncio
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import approval_gate as ag  # noqa: E402  (imported before the stubs so its state is the shared one)
from src.foundation import audit  # noqa: E402

OWNER = "424242"


def _load_listener():
    """Import services/telegram/listener.py with heavyweight siblings stubbed (httpx, the services package
    __init__ which pulls in search deps, core.middleware). Everything the stubs add is removed again afterwards."""
    stubs = {}
    stubs["httpx"] = types.ModuleType("httpx")
    core = types.ModuleType("core")
    core.__path__ = []
    mw = types.ModuleType("core.middleware")
    mw.INTERNAL_TOOL_HEADER, mw.INTERNAL_TOOL_TOKEN = "x-internal", "t"
    core.middleware = mw
    stubs["core"], stubs["core.middleware"] = core, mw
    svc = types.ModuleType("services")
    svc.__path__ = [os.path.join(ROOT, "services")]
    stubs["services"] = svc
    with mock.patch.dict(sys.modules, stubs):
        for name in [n for n in sys.modules if n.startswith("services.")]:
            del sys.modules[name]
        import importlib
        listener = importlib.import_module("services.telegram.listener")
        return listener


class CallbackTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.L = _load_listener()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name, "TELEGRAM_BOT_TOKEN": "1:x",
                                                "TELEGRAM_CHAT_ID": OWNER, "TELEGRAM_ENABLED": "true"})
        self.env.start()
        ag.reset_state()
        self.api = []
        self.lst = self.L.TelegramListener()
        self.lst._api = lambda method, payload: (self.api.append((method, payload)) or {"ok": True})

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
        ag.reset_state()

    def _pending(self, approval_id="abc123"):
        p = ag.PendingApproval(id=approval_id, tool="bash", preview="rm x", session_id="s", owner="o",
                               created=0.0, future=asyncio.get_running_loop().create_future(), key="k")
        ag._pending[approval_id] = p
        return p

    def _cq(self, data, sender=OWNER, chat=OWNER, message_id=77):
        return {"id": "cq1", "from": {"id": int(sender) if sender else None}, "data": data,
                "message": {"message_id": message_id, "chat": {"id": int(chat) if chat else None}}}

    async def _press(self, cq):
        await self.lst._handle_callback(cq)
        await asyncio.sleep(0)                                   # approval_gate._settle completes the future via call_soon

    def _methods(self):
        return [m for m, _ in self.api]

    async def test_owner_approve_press(self):
        p = self._pending()
        await self._press(self._cq("approve:abc123"))
        self.assertTrue(p.future.done() and p.future.result() is True)
        self.assertEqual(self._methods(), ["answerCallbackQuery", "editMessageReplyMarkup"])
        self.assertIn("Approved", self.api[0][1]["text"])
        self.assertEqual(self.api[1][1]["reply_markup"], {"inline_keyboard": []})        # buttons removed
        self.assertEqual(self.api[1][1]["message_id"], 77)
        self.assertTrue(any(r.get("resolver") == "telegram-button" for r in audit.recent(5)))

    async def test_owner_deny_press(self):
        p = self._pending()
        await self._press(self._cq("deny:abc123"))
        self.assertTrue(p.future.done() and p.future.result() is False)
        self.assertIn("Denied", self.api[0][1]["text"])

    async def test_someone_else_pressing_does_nothing(self):
        p = self._pending()
        await self._press(self._cq("approve:abc123", sender="999"))
        self.assertFalse(p.future.done())
        self.assertEqual(self._methods(), ["answerCallbackQuery"])                       # silent ack, nothing else
        self.assertNotIn("text", self.api[0][1])

    async def test_right_person_wrong_chat_does_nothing(self):
        p = self._pending()
        await self._press(self._cq("approve:abc123", chat="-100555"))       # e.g. the bot added to a group
        self.assertFalse(p.future.done())
        self.assertEqual(self._methods(), ["answerCallbackQuery"])

    async def test_no_owner_configured_means_nobody_may_press(self):
        p = self._pending()
        self.lst.chat_id = ""
        await self._press(self._cq("approve:abc123", sender="", chat=""))
        self.assertFalse(p.future.done())

    async def test_malformed_or_hostile_data_is_rejected(self):
        p = self._pending()
        for data in ("approve:abc12", "approve:ABC123", "approve:abc123 ", "approve:abc123;deny:all", "deny:all",
                     "approve:../../x", "", None, "approve:abc1234", "mission:m-abc123", "stop"):
            self.api.clear()
            await self._press(self._cq(data))
            self.assertEqual(self._methods(), ["answerCallbackQuery"], repr(data))
            self.assertEqual(self.api[0][1].get("text"), "Unknown button", repr(data))
        self.assertFalse(p.future.done())

    async def test_button_for_an_expired_request_is_harmless(self):
        await self._press(self._cq("approve:abc123"))
        self.assertIn("No pending approval", self.api[0][1]["text"])

    async def test_second_tap_cannot_double_approve(self):
        p = self._pending()
        await self._press(self._cq("approve:abc123"))
        self.api.clear()
        await self._press(self._cq("deny:abc123"))
        self.assertTrue(p.future.result() is True)                                       # first decision stands
        self.assertIn("No pending approval", self.api[0][1]["text"])

    async def test_loop_dispatches_callback_updates_and_asks_telegram_for_them(self):
        p = self._pending()
        seen_payloads = []
        real_get = self.lst._get_updates
        calls = {"n": 0}

        async def fake_get_updates():
            calls["n"] += 1
            if calls["n"] == 1:
                return [{"update_id": 5, "callback_query": self._cq("approve:abc123")}]
            raise asyncio.CancelledError()

        self.lst._get_updates = fake_get_updates
        await self.lst._loop()
        await asyncio.sleep(0)
        self.assertTrue(p.future.done() and p.future.result() is True)
        self.assertEqual(self.lst._offset, 6)

        # the real request must include callback_query, or Telegram never delivers button presses
        self.lst._api = lambda method, payload: (seen_payloads.append(payload) or {"result": []})
        self.lst._get_updates = real_get
        await self.lst._get_updates()
        self.assertIn("callback_query", seen_payloads[0]["allowed_updates"])
        self.assertIn("message", seen_payloads[0]["allowed_updates"])


class ButtonPressTests(CallbackTests):
    """Tap-instead-of-type buttons (callback data `do:<command>`): same owner check, same parser, allow-listed kinds only, one tap."""

    def setUp(self):
        super().setUp()
        self.handled = []

        async def fake_handle(kind, arg):
            self.handled.append((kind, arg))
        self.lst._handle_foundation = fake_handle

    async def _tap(self, data, **kw):
        await self.lst._handle_callback(self._cq(data, **kw))
        for t in list(self.lst._control_tasks):
            await t

    async def test_owner_tap_runs_the_command_once_and_removes_the_buttons(self):
        await self._tap("do:yes g-abc123")
        self.assertEqual(self.handled, [("fix_yes", "g-abc123")])
        self.assertEqual(self._methods(), ["answerCallbackQuery", "editMessageReplyMarkup"])
        self.assertEqual(self.api[1][1]["reply_markup"], {"inline_keyboard": []})

    async def test_menu_taps(self):
        for data, want in (("do:all", ("run_all", "")), ("do:games refresh", ("games_refresh", "")), ("do:fast lane off", ("fastlane", "off")),
                           ("do:dashboard", ("dashboard", ""))):
            self.handled.clear()
            await self._tap(data)
            self.assertEqual(self.handled, [want], data)

    async def test_someone_else_tapping_does_nothing(self):
        await self._tap("do:yes g-abc123", sender="999")
        self.assertEqual(self.handled, [])
        self.assertEqual(self._methods(), ["answerCallbackQuery"])
        await self._tap("do:yes g-abc123", chat="999")
        self.assertEqual(self.handled, [])

    async def test_commands_that_are_not_allowed_or_unparseable_do_nothing(self):
        for data in ("do:stop", "do:authorize live-trading", "do:forge: x", "do:hello there friend", "do:mission: do a thing"):
            self.api.clear()
            await self._tap(data)
            self.assertEqual(self.handled, [], data)
            self.assertEqual(self._methods(), ["answerCallbackQuery"], data)
            self.assertEqual(self.api[0][1]["text"], "Unknown button")

    async def test_the_old_approve_deny_buttons_still_work(self):
        p = self._pending()
        await self._press(self._cq("approve:abc123"))
        self.assertTrue(p.future.done() and p.future.result() is True)
        self.assertEqual(self.handled, [])

    async def test_notify_sends_the_keyboard_when_given_one(self):
        sent = []
        self.lst._service = types.SimpleNamespace(send=lambda text, **kw: sent.append((text, kw)))
        await self.lst._notify("hello")
        await self.lst._notify("pick", {"inline_keyboard": [[{"text": "a", "callback_data": "do:all"}]]})
        self.assertEqual(sent[0], ("hello", {}))
        self.assertEqual(sent[1][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "do:all")


class AgentLoopFormTests(unittest.IsolatedAsyncioTestCase):
    """Found live 2026-09-23: a mission needing web_search (reynolds_research, real_estate_leads, social_content, ct_to_print's
    cheapest-producer search...) blocked at step 1 with "the tool is not available" - allow_web_search was never sent, so
    /api/chat_stream disabled it before the mission's own contract ever got a say. Same fix as the pre-existing allow_bash."""

    @classmethod
    def setUpClass(cls):
        cls.L = _load_listener()

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "1:x", "TELEGRAM_CHAT_ID": OWNER, "TELEGRAM_ENABLED": "true"})
        self.env.start()
        self.lst = self.L.TelegramListener()

    def tearDown(self):
        self.env.stop()

    async def test_the_agent_loop_call_enables_both_bash_and_web_search(self):
        captured = {}

        class FakeStream:
            def __init__(self, method, url, **kw):
                captured["form"] = kw.get("data")

            async def __aenter__(self):
                resp = mock.Mock()
                resp.raise_for_status = mock.Mock()

                async def aiter_lines():
                    yield "data: [DONE]"
                resp.aiter_lines = aiter_lines
                return resp

            async def __aexit__(self, *a):
                return False

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def stream(self, method, url, **kw):
                return FakeStream(method, url, **kw)

        with mock.patch.object(self.L.httpx, "AsyncClient", FakeClient, create=True), \
             mock.patch.object(self.L, "_internal_headers", return_value={}):
            await self.lst._call_agent_loop("hello")
        self.assertEqual(captured["form"]["allow_bash"], "true")
        self.assertEqual(captured["form"]["allow_web_search"], "true")


class DynamicSessionIdTests(unittest.IsolatedAsyncioTestCase):
    """The chat session sent to /api/chat_stream must be read live (session_rotation.current_session_id()),
    not a module-level constant frozen at import time - otherwise a daily rotation would need a process
    restart to take effect. Found while building the rotation feature 2026-09-24."""

    @classmethod
    def setUpClass(cls):
        cls.L = _load_listener()

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "1:x", "TELEGRAM_CHAT_ID": OWNER, "TELEGRAM_ENABLED": "true"})
        self.env.start()
        self.lst = self.L.TelegramListener()

    def tearDown(self):
        self.env.stop()

    async def test_a_rotated_session_is_used_without_a_restart(self):
        captured = {}

        class FakeStream:
            def __init__(self, method, url, **kw):
                captured["form"] = kw.get("data")

            async def __aenter__(self):
                resp = mock.Mock()
                resp.raise_for_status = mock.Mock()

                async def aiter_lines():
                    yield "data: [DONE]"
                resp.aiter_lines = aiter_lines
                return resp

            async def __aexit__(self, *a):
                return False

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def stream(self, method, url, **kw):
                return FakeStream(method, url, **kw)

        with mock.patch.object(self.L.httpx, "AsyncClient", FakeClient, create=True), \
             mock.patch.object(self.L, "_internal_headers", return_value={}), \
             mock.patch.object(self.L.session_rotation, "current_session_id", return_value="rotated-session-42"):
            await self.lst._call_agent_loop("hello")
        self.assertEqual(captured["form"]["session"], "rotated-session-42")


if __name__ == "__main__":
    unittest.main()
