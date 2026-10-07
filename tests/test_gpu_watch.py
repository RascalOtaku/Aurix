"""GPU PC watch: models gone missing get one clear message; the PC being off is recorded, never paged."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.foundation import gpu_watch  # noqa: E402


def ollama(models=None, loaded=()):
    """A fake GPU PC: models=None means it is not answering."""
    def get(url):
        if models is None:
            raise OSError("no route to host")
        if url.endswith("/api/tags"):
            return 200, json.dumps({"models": [{"name": m} for m in models]}).encode()
        if url.endswith("/api/ps"):
            return 200, json.dumps({"models": [{"name": m} for m in loaded]}).encode()
        return 404, b""
    return get


class GpuWatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name, "AURIX_GPU_HOST": "100.64.0.10",
                                         "AURIX_GPU_OLLAMA_URL": ""})
        p.start()
        self.addCleanup(p.stop)
        self.t = 1_000_000.0

    def tick(self, get):
        self.t += gpu_watch.CHECK_EVERY + 1
        return gpu_watch.check(self.t, get)

    def test_missing_models_get_one_message_then_a_back_notice(self):
        self.assertEqual(self.tick(ollama(["qwen3.5:9b", "llama3.2:3b"])), [])
        msg = self.tick(ollama([]))                                                  # answering, but the list is empty
        self.assertEqual(len(msg), 1)
        self.assertIn("llama3.2:3b, qwen3.5:9b", msg[0])
        self.assertIn("aurix-gpu-pc-setup.ps1", msg[0])
        self.assertEqual(self.tick(ollama([])), [])                                  # not repeated
        back = self.tick(ollama(["qwen3.5:9b", "llama3.2:3b"]))
        self.assertEqual(len(back), 1)
        self.assertIn("back", back[0])

    def test_being_off_is_recorded_never_paged(self):
        self.tick(ollama(["qwen3.5:9b"]))
        for _ in range(3):
            self.assertEqual(self.tick(ollama(None)), [])
            self.tick(ollama(["qwen3.5:9b"]))
        text = gpu_watch.status_text(self.t + 1, ollama(["qwen3.5:9b"], loaded=["qwen3.5:9b"]))
        self.assertIn("Went offline 3 times in 7 days", text)
        self.assertIn("Loaded in VRAM now: qwen3.5:9b", text)

    def test_checks_are_spaced_and_forget_works(self):
        self.tick(ollama(["a:1", "b:2"]))
        self.assertEqual(gpu_watch.check(self.t + 5, ollama([])), [])               # too soon: not even checked
        self.assertIn("Stopped tracking", gpu_watch.forget("b:2"))
        msg = self.tick(ollama(["a:1"]))
        self.assertEqual(msg, [])                                                    # b:2 was deleted on purpose
        self.assertIn("was not tracking", gpu_watch.forget("zzz"))

    def test_off_without_settings(self):
        with mock.patch.dict(os.environ, {"AURIX_GPU_HOST": ""}):
            self.assertEqual(gpu_watch.check(self.t, ollama([])), [])
            self.assertIn("not set up", gpu_watch.status_text())


if __name__ == "__main__":
    unittest.main()
