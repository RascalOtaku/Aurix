"""Tests for src/remote_inference.py (stdlib only; runs under unittest or pytest)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import remote_inference as ri  # noqa: E402


class RemoteDetectionTests(unittest.TestCase):
    def test_remote_hosts(self):
        for url in ("http://100.64.0.10:11434/v1",
                    "http://100.64.0.10:11434/v1/chat/completions",
                    "http://gpu-box:11434/v1",
                    "http://10.0.0.46:11434/v1"):
            self.assertTrue(ri.is_remote_ollama(url), url)

    def test_local_hosts(self):
        for url in ("http://127.0.0.1:11434/v1", "http://localhost:11434/v1",
                    "http://host.docker.internal:11434/v1", "http://ollama:11434/v1",
                    "127.0.0.1:11434"):
            self.assertFalse(ri.is_remote_ollama(url), url)

    def test_non_ollama_endpoints_untouched(self):
        for url in ("https://api.openai.com/v1", "http://100.1.2.3:8000/v1", "", None):
            self.assertFalse(ri.is_remote_ollama(url), url)


class PickTests(unittest.TestCase):
    def _env(self, value):
        return mock.patch.dict(os.environ, {"AURIX_REMOTE_MODELS": value})

    def test_unset_returns_none(self):
        with self._env(""):
            self.assertIsNone(ri.pick_remote_model(0.9, True))
            self.assertEqual(ri.remote_models(), [])

    def test_agent_mode_uses_largest_available(self):
        with self._env("qwen2.5:7b, qwen2.5:3b"):
            self.assertEqual(ri.remote_models(), ["qwen2.5:3b", "qwen2.5:7b"])
            self.assertEqual(ri.pick_remote_model(0.10, agent_mode=True), "qwen2.5:7b")

    def test_never_picks_a_model_the_remote_lacks(self):
        with self._env("qwen2.5:3b,qwen2.5:7b"):
            for sig in (0.0, 0.1, 0.35, 0.75, 1.0):
                for agent in (True, False):
                    self.assertIn(ri.pick_remote_model(sig, agent), ("qwen2.5:3b", "qwen2.5:7b"))
                    self.assertNotEqual(ri.pick_remote_model(sig, agent), "qwen2.5:14b")

    def test_chat_ladder(self):
        with self._env("qwen2.5:3b,qwen2.5:7b"):
            self.assertEqual(ri.pick_remote_model(0.10, agent_mode=False), "qwen2.5:3b")
            self.assertEqual(ri.pick_remote_model(0.35, agent_mode=False), "qwen2.5:7b")
            self.assertEqual(ri.pick_remote_model(0.75, agent_mode=False), "qwen2.5:7b")

    def test_single_model(self):
        with self._env("qwen2.5:7b"):
            self.assertEqual(ri.pick_remote_model(0.0, False), "qwen2.5:7b")
            self.assertEqual(ri.pick_remote_model(1.0, True), "qwen2.5:7b")

    def test_size_ordering_beats_list_order_and_ties_favor_later(self):
        with self._env("qwen2.5:14b,qwen2.5:3b,qwen2.5-coder:7b,qwen2.5:7b"):
            self.assertEqual(ri.remote_models(),
                             ["qwen2.5:3b", "qwen2.5-coder:7b", "qwen2.5:7b", "qwen2.5:14b"])
        with self._env("qwen2.5:32b,qwen2.5:7b"):
            self.assertEqual(ri.pick_remote_model(0.9, True), "qwen2.5:32b")


if __name__ == "__main__":
    unittest.main()
