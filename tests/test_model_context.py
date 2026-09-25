"""Tests for model_context.py — local endpoint detection, token estimation, known model lookup."""

import pytest

from src.model_context import _is_local_endpoint, estimate_tokens, _lookup_known, _query_context_length


class TestIsLocalEndpoint:
    def test_localhost(self):
        assert _is_local_endpoint("http://localhost:5000/v1/chat/completions") is True

    def test_loopback_ipv4(self):
        assert _is_local_endpoint("http://127.0.0.1:8080/v1/chat/completions") is True

    def test_private_192_168(self):
        assert _is_local_endpoint("http://192.168.1.1:11434/v1/chat/completions") is True

    def test_private_10(self):
        assert _is_local_endpoint("http://10.0.0.5:8000/v1/chat/completions") is True

    def test_tailscale_100(self):
        # 100.64.0.0/10 is the CGNAT range Tailscale uses.
        assert _is_local_endpoint("http://100.64.0.1:5000/v1/chat/completions") is True

    def test_openai_is_remote(self):
        assert _is_local_endpoint("https://api.openai.com/v1/chat/completions") is False

    def test_anthropic_is_remote(self):
        assert _is_local_endpoint("https://api.anthropic.com/v1/messages") is False

    def test_empty_url(self):
        assert _is_local_endpoint("") is False

    def test_malformed_url(self):
        assert _is_local_endpoint("not-a-url") is False


class TestEstimateTokens:
    def test_empty_list(self):
        assert estimate_tokens([]) == 0

    def test_single_short_message(self):
        messages = [{"role": "user", "content": "Hello"}]
        tokens = estimate_tokens(messages)
        # 4 overhead + int(5 * 0.3) = 4 + 1 = 5
        assert tokens == 5

    def test_multiple_messages(self):
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi there"},
        ]
        tokens = estimate_tokens(messages)
        assert tokens > 0
        # Each message adds 4 overhead + chars * 0.3
        assert tokens == 4 + int(16 * 0.3) + 4 + int(8 * 0.3)

    def test_multimodal_content_list(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this image"},
                    {"type": "image_url", "image_url": {"url": "data:..."}},
                ],
            }
        ]
        tokens = estimate_tokens(messages)
        # 4 overhead + int(19 * 0.3) for the text item; image_url is ignored
        assert tokens == 4 + int(19 * 0.3)

    def test_missing_content_key(self):
        messages = [{"role": "assistant"}]
        tokens = estimate_tokens(messages)
        # 4 overhead + 0 content
        assert tokens == 4

    def test_scales_with_length(self):
        short = estimate_tokens([{"role": "user", "content": "short"}])
        long_text = "a" * 10000
        long = estimate_tokens([{"role": "user", "content": long_text}])
        assert long > short * 10


class TestLookupKnown:
    def test_claude_sonnet(self):
        assert _lookup_known("claude-sonnet-4-5") == 200000

    def test_gpt4o(self):
        assert _lookup_known("gpt-4o") == 128000

    def test_deepseek_r1(self):
        assert _lookup_known("deepseek-r1") == 64000

    def test_gemini_pro(self):
        assert _lookup_known("gemini-2.5-pro") == 1048576

    def test_unknown_model(self):
        assert _lookup_known("totally-unknown-model-xyz") is None

    def test_namespaced_model(self):
        """Models prefixed with provider/ should still match."""
        result = _lookup_known("openrouter/deepseek-r1")
        assert result == 64000

    def test_model_with_tag(self):
        """Models with :free or :extended suffixes should still match."""
        result = _lookup_known("deepseek-r1:free")
        assert result == 64000


class _FakeResponse:
    def __init__(self, json_body, is_success=True):
        self._json = json_body
        self.is_success = is_success

    def json(self):
        return self._json


class TestQueryContextLengthOllama:
    """Real bug found live 2026-09-24: KNOWN_CONTEXT_WINDOWS['qwen2.5'] = 131072 is that model
    FAMILY's max, but a real qwen2.5:7b Ollama pull's own registered context is 32768 (confirmed
    via a live /api/show call) - using the family max meant a 6-day, 727k-cumulative-token Telegram
    session never looked "full enough" to compact. Ollama's OpenAI-compat /v1/models does not expose
    context length at all, so /api/show must be queried directly for local Ollama endpoints."""

    def test_prefers_ollamas_own_api_show_value_over_the_family_max_table(self, monkeypatch):
        import httpx as httpx_module

        def fake_get(url, timeout=None):
            assert url.endswith("/slots")
            return _FakeResponse({}, is_success=False)               # not a llama.cpp server

        def fake_post(url, json=None, timeout=None):
            assert url.endswith("/api/show")
            assert json == {"model": "qwen2.5:7b"}
            return _FakeResponse({"model_info": {"qwen2.context_length": 32768}})

        monkeypatch.setattr(httpx_module, "get", fake_get)
        monkeypatch.setattr(httpx_module, "post", fake_post)

        assert _query_context_length("http://100.64.0.10:11434/v1/chat/completions", "qwen2.5:7b") == 32768

    def test_falls_back_to_the_known_table_when_ollama_has_nothing_to_say(self, monkeypatch):
        import httpx as httpx_module

        monkeypatch.setattr(httpx_module, "get", lambda url, timeout=None: _FakeResponse({}, is_success=False))
        monkeypatch.setattr(httpx_module, "post", lambda url, json=None, timeout=None: _FakeResponse({}, is_success=False))

        assert _query_context_length("http://100.64.0.10:11434/v1/chat/completions", "qwen2.5:7b") == 131072

    def test_never_queries_api_show_for_a_remote_endpoint(self, monkeypatch):
        import httpx as httpx_module

        def boom(*a, **k):
            raise AssertionError("must not call a remote host's /api/show or /slots")

        monkeypatch.setattr(httpx_module, "get", boom)
        monkeypatch.setattr(httpx_module, "post", boom)

        assert _query_context_length("https://api.openai.com/v1/chat/completions", "gpt-4o") == 128000
