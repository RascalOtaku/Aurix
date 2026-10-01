"""Regression 2026-09-28: one process-wide httpx.AsyncClient was bound to the first event loop that used it; once that loop
closed, every later model call from another loop failed with "Event loop is closed" (unanswered Telegram messages)."""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import llm_core  # noqa: E402


class ClientPerLoop(unittest.TestCase):
    def test_each_event_loop_gets_its_own_usable_client(self):
        async def grab():
            c = llm_core._get_http_client()
            self.assertIs(c, llm_core._get_http_client())          # reused within one loop
            return c
        first = asyncio.run(grab())                                  # this loop is closed when asyncio.run returns
        second = asyncio.run(grab())
        self.assertIsNot(first, second)
        self.assertFalse(second.is_closed)

    def test_clients_of_closed_loops_are_dropped(self):
        for _ in range(3):
            asyncio.run(asyncio.sleep(0))
            asyncio.run(self._touch())
        self.assertLessEqual(len(llm_core._http_clients), 1)

    async def _touch(self):
        llm_core._get_http_client()


if __name__ == "__main__":
    unittest.main()


class FallbackFailsFast(unittest.TestCase):
    """Regression 2026-09-28: a CPU fallback sat 10 minutes on an agent prompt and produced nothing. A fallback must show output
    within FALLBACK_FIRST_OUTPUT_SECONDS or fail with a plain error naming the model."""

    def _run(self, fallback_behaviour):
        from unittest import mock

        async def fake_stream(url, model, messages, **kw):
            if model == "primary":
                yield 'event: error\ndata: {"error": "Event loop is closed", "status": 502}\n\n'
                return
            if fallback_behaviour == "hang":
                await asyncio.sleep(30)
            yield 'data: {"delta": "hello"}\n\n'
            yield "data: [DONE]\n\n"

        async def collect():
            return [c async for c in llm_core.stream_llm_with_fallback([("u", "primary", {}), ("u", "cpu-fallback", {})], [])]
        with mock.patch.object(llm_core, "stream_llm", fake_stream), mock.patch.object(llm_core, "FALLBACK_FIRST_OUTPUT_SECONDS", 0.2):
            return asyncio.run(collect())

    def test_a_silent_fallback_fails_fast_with_a_named_error(self):
        out = self._run("hang")
        self.assertEqual(len(out), 1)
        self.assertIn("fallback model cpu-fallback produced no output", out[0])

    def test_a_working_fallback_still_answers(self):
        out = self._run("answer")
        self.assertIn('data: {"delta": "hello"}\n\n', out)
