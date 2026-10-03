"""Regression tests for ChatHandler's pure helpers in src/chat_handler.

Covers preset validation/extraction (shared by /api/chat and /api/chat_stream)
and the small session helpers. Heavy collaborators (managers, DB) are faked;
only the logic under test runs.
"""
import os
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# src.chat_handler does `from core.models import ChatMessage`, which would
# trigger core/__init__.py -> core.auth -> bcrypt (not installed in the test
# env). Stub core/core.models only for the duration of this import, so the
# stub can't leak into other test modules (pytest imports all test modules
# up front during collection).
_core_stub = types.ModuleType("core")
_models_stub = types.ModuleType("core.models")


class ChatMessage:
    def __init__(self, role, content):
        self.role = role
        self.content = content


_models_stub.ChatMessage = ChatMessage

with mock.patch.dict(sys.modules, {"core": _core_stub, "core.models": _models_stub}):
    from src import chat_handler as ch  # noqa: E402

from fastapi import HTTPException  # noqa: E402
from src.constants import DEFAULT_MAX_TOKENS, DEFAULT_TEMPERATURE, MAX_CONTEXT_MESSAGES  # noqa: E402


def make_handler(presets):
    return ch.ChatHandler(
        session_manager=None,
        memory_manager=None,
        chat_processor=None,
        research_handler=None,
        preset_manager=SimpleNamespace(presets=presets),
        upload_handler=None,
    )


class ValidatePresetTests(unittest.TestCase):
    def test_none_falls_back_to_aurix_preset(self):
        h = make_handler({"aurix": {"system_prompt": "Hi."}})
        temp, tokens, prompt, name = h.validate_and_extract_preset(None)
        self.assertEqual(prompt, "Hi.")
        self.assertEqual(name, "")
        self.assertEqual(temp, DEFAULT_TEMPERATURE)
        self.assertEqual(tokens, DEFAULT_MAX_TOKENS)

    def test_empty_string_falls_back_to_aurix_preset(self):
        h = make_handler({"aurix": {}})
        temp, tokens, prompt, name = h.validate_and_extract_preset("")
        self.assertIsNone(prompt)
        self.assertEqual(temp, DEFAULT_TEMPERATURE)

    def test_unknown_preset_raises_400(self):
        h = make_handler({"aurix": {}})
        with self.assertRaises(HTTPException) as ctx:
            h.validate_and_extract_preset("nope")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_missing_default_preset_raises_400(self):
        # Documented behavior: the "aurix" default must exist in presets.
        h = make_handler({})
        with self.assertRaises(HTTPException) as ctx:
            h.validate_and_extract_preset(None)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_disabled_preset_returns_defaults(self):
        h = make_handler({"p": {"enabled": False, "system_prompt": "X",
                                "temperature": 0.9, "character_name": "Bob"}})
        temp, tokens, prompt, name = h.validate_and_extract_preset("p")
        self.assertEqual((temp, tokens, prompt, name),
                         (DEFAULT_TEMPERATURE, DEFAULT_MAX_TOKENS, None, ""))

    def test_character_name_prepended_to_prompt(self):
        h = make_handler({"p": {"character_name": "Bob", "system_prompt": "Be nice."}})
        _, _, prompt, name = h.validate_and_extract_preset("p")
        self.assertEqual(name, "Bob")
        self.assertEqual(prompt, "Your name is Bob. Be nice.")

    def test_character_name_without_prompt(self):
        h = make_handler({"p": {"character_name": "Bob"}})
        _, _, prompt, name = h.validate_and_extract_preset("p")
        self.assertEqual(prompt, "Your name is Bob.")

    def test_prompt_without_character_name(self):
        h = make_handler({"p": {"system_prompt": "Be nice."}})
        _, _, prompt, name = h.validate_and_extract_preset("p")
        self.assertEqual(prompt, "Be nice.")
        self.assertEqual(name, "")

    def test_temperature_and_max_tokens_overrides(self):
        h = make_handler({"p": {"temperature": 0.1, "max_tokens": 500}})
        temp, tokens, _, _ = h.validate_and_extract_preset("p")
        self.assertEqual(temp, 0.1)
        self.assertEqual(tokens, 500)


class SessionHelperTests(unittest.TestCase):
    def setUp(self):
        self.h = make_handler({})

    def test_update_session_name_derives_from_first_five_words(self):
        s = SimpleNamespace(name="", history=[])
        self.h.update_session_name_if_needed(s, "hello world foo bar baz qux extra")
        self.assertEqual(s.name, "Chat: hello world foo bar baz")

    def test_update_session_name_keeps_existing_name(self):
        s = SimpleNamespace(name="Keep me", history=[])
        self.h.update_session_name_if_needed(s, "hello world")
        self.assertEqual(s.name, "Keep me")

    def test_update_session_name_empty_message(self):
        s = SimpleNamespace(name="", history=[])
        self.h.update_session_name_if_needed(s, "   ")
        self.assertEqual(s.name, "Chat")

    def test_trim_history_keeps_last_n(self):
        history = list(range(MAX_CONTEXT_MESSAGES + 10))
        s = SimpleNamespace(name="", history=history)
        self.h.trim_history_if_needed(s)
        self.assertEqual(len(s.history), MAX_CONTEXT_MESSAGES)
        self.assertEqual(s.history, list(range(10, MAX_CONTEXT_MESSAGES + 10)))

    def test_trim_history_leaves_short_history_alone(self):
        history = [1, 2, 3]
        s = SimpleNamespace(name="", history=history)
        self.h.trim_history_if_needed(s)
        self.assertEqual(s.history, [1, 2, 3])

    def test_enhance_message_is_identity(self):
        self.assertEqual(self.h.enhance_message_if_needed("hello"), "hello")


if __name__ == "__main__":
    unittest.main()
