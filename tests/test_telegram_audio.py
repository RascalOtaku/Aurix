"""The Telegram listener routes voice notes, audio files, and audio-typed documents to the transcription pipeline instead of the
chat agent - checked at the dispatch layer only; src/foundation/transcription.py itself is tested in test_foundation_transcription.py."""
import asyncio
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import approval_gate as ag  # noqa: E402
from src.foundation import audit  # noqa: E402

OWNER = "424242"


def _load_listener():
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


class AudioDispatchTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.L = _load_listener()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name, "TELEGRAM_BOT_TOKEN": "1:x",
                                                "TELEGRAM_CHAT_ID": OWNER, "TELEGRAM_ENABLED": "true"})
        self.env.start()
        ag.reset_state()
        audit._heads.clear()
        self.lst = self.L.TelegramListener()
        self.sent = []
        self.lst._service.send = lambda text, **k: self.sent.append(text)
        self.sent_docs = []
        self.lst._service.send_document = lambda path, caption="": self.sent_docs.append((path, caption))

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
        ag.reset_state()
        audit._heads.clear()

    async def test_a_voice_note_is_routed_to_transcription_not_chat(self):
        from src.foundation import transcription
        with mock.patch.object(transcription, "request", return_value="🎙️ Got it") as req:
            await self.lst._handle_audio({"voice": {"file_id": "f1", "duration": 12}})
        self.assertEqual(req.call_args[0][0], "f1")
        self.assertIn("Got it", self.sent[-1])

    async def test_an_audio_message_is_routed(self):
        from src.foundation import transcription
        with mock.patch.object(transcription, "request", return_value="ok") as req:
            await self.lst._handle_audio({"audio": {"file_id": "f2", "file_name": "song.mp3", "duration": 90}})
        self.assertEqual(req.call_args[0][:2], ("f2", "song.mp3"))
        self.assertEqual(req.call_args[0][2], 90)

    async def test_an_audio_typed_document_is_routed(self):
        from src.foundation import transcription
        with mock.patch.object(transcription, "request", return_value="ok") as req:
            await self.lst._handle_audio({"document": {"file_id": "f3", "file_name": "memo.m4a", "mime_type": "audio/mp4"}})
        self.assertEqual(req.call_args[0][0], "f3")

    async def test_a_non_audio_document_is_left_alone(self):
        from src.foundation import transcription
        with mock.patch.object(transcription, "request") as req:
            await self.lst._handle_audio({"document": {"file_id": "f4", "file_name": "notes.pdf", "mime_type": "application/pdf"}})
        req.assert_not_called()
        self.assertEqual(self.sent, [])

    async def test_the_main_loop_dispatches_voice_before_the_text_path(self):
        """A message with `voice` set never reaches the plain-text/control-command path."""
        update = {"update_id": 1, "message": {"chat": {"id": int(OWNER)}, "voice": {"file_id": "f5", "duration": 5}}}
        calls = {"n": 0}

        async def fake_updates():
            calls["n"] += 1
            await asyncio.sleep(0)                                  # a real suspension point so the loop cannot busy-spin the event loop
            if calls["n"] > 1:
                raise asyncio.CancelledError                        # `_loop()` catches this itself and returns cleanly (see its except clause)
            return [update]
        self.lst._get_updates = fake_updates
        from src.foundation import transcription
        with mock.patch.object(transcription, "request", return_value="queued") as req:
            await asyncio.wait_for(self.lst._loop(), timeout=5)
            await asyncio.sleep(0)                                  # let the task _loop() scheduled for the update actually run
        self.assertTrue(req.called)


if __name__ == "__main__":
    unittest.main()
