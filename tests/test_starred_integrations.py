"""Integrations from the owner's starred repos: key rotation, free-model routing, MCP presets, skills, extras."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import key_rotation, mcp_auto_discover, mcp_presets, model_router  # noqa: E402
from src.foundation import capabilities, skills  # noqa: E402


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class KeyPoolTests(unittest.TestCase):
    def test_rotates_on_quota_and_recovers_after_cooldown(self):
        clock = FakeClock()
        p = key_rotation.KeyPool(["a", "b", "a", ""], cooldown=600, clock=clock)
        self.assertEqual(p.keys, ["a", "b"])                  # deduped, blanks dropped
        self.assertEqual(p.current(), "a")
        p.mark_spent("a")
        self.assertEqual(p.current(), "b")
        p.mark_spent("b")
        self.assertEqual(p.current(), "a")                    # all spent: the one that frees up first
        clock.t += 601
        self.assertFalse(p.is_cooling("a"))

    def test_caps_the_number_of_keys(self):
        self.assertEqual(len(key_rotation.KeyPool([str(i) for i in range(9)]).keys), key_rotation.MAX_KEYS)


class ApplyReportTests(unittest.TestCase):
    def setUp(self):
        key_rotation.reset()
        self.addCleanup(key_rotation.reset)

    def env(self, **kw):
        patcher = mock.patch.dict(os.environ, kw, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("OPENROUTER_API_KEY", "OPENROUTER_API_KEYS", "OPENCODE_ZEN_API_KEY", "OPENCODE_ZEN_API_KEYS"):
            if name not in kw:
                os.environ.pop(name, None)

    def test_fills_a_missing_key_and_rotates_after_429(self):
        self.env(OPENROUTER_API_KEYS="k1,k2")
        h = key_rotation.apply("openrouter", {})
        self.assertEqual(h["Authorization"], "Bearer k1")
        self.assertTrue(key_rotation.report("openrouter", h, 429))
        self.assertEqual(key_rotation.apply("openrouter", dict(h))["Authorization"], "Bearer k2")

    def test_never_touches_an_endpoints_own_key(self):
        self.env(OPENROUTER_API_KEYS="k1,k2")
        h = {"Authorization": "Bearer mine"}
        self.assertEqual(key_rotation.apply("openrouter", dict(h)), h)
        self.assertFalse(key_rotation.report("openrouter", h, 429))

    def test_ignores_non_quota_errors_and_unconfigured_providers(self):
        self.env(OPENROUTER_API_KEY="solo")
        h = key_rotation.apply("openrouter", {})
        self.assertFalse(key_rotation.report("openrouter", h, 500))
        self.assertEqual(key_rotation.apply("openai", {}), {})

    def test_llm_core_detects_zen_and_uses_the_pool(self):
        self.env(OPENCODE_ZEN_API_KEY="zen1")
        from src import llm_core
        self.assertEqual(llm_core._detect_provider("https://opencode.ai/zen/v1/chat/completions"), "opencode")
        h = llm_core._provider_headers("opencode", None)
        self.assertEqual(h["Authorization"], "Bearer zen1")


class FreeRoutingTests(unittest.TestCase):
    MODELS = ["gpt-4o", "meta-llama/llama-3.3-70b-instruct:free", "qwen/qwen3-coder:free", "openrouter/free"]

    def test_free_strategy_only_picks_free_models(self):
        self.assertEqual(model_router.choose_model_for_task("fix this bug", self.MODELS, strategy="free"),
                         "qwen/qwen3-coder:free")
        self.assertEqual(model_router.choose_model_for_task("tell me a story", self.MODELS, strategy="free"),
                         "openrouter/free")

    def test_free_strategy_falls_back_when_nothing_is_free(self):
        self.assertEqual(model_router.choose_model_for_task("fix this bug", ["gpt-4.1"], strategy="free"), "gpt-4.1")

    def test_balanced_strategy_is_unchanged(self):
        self.assertEqual(model_router.choose_model_for_task("fix this bug", self.MODELS), "gpt-4o")

    def test_fallback_chain_lists_every_free_model_once(self):
        chain = model_router.free_fallback_chain(self.MODELS + ["big-pickle"], "refactor the parser")
        self.assertEqual(chain[0], "qwen/qwen3-coder:free")
        self.assertEqual(sorted(chain), sorted(m for m in self.MODELS + ["big-pickle"] if m != "gpt-4o"))


class McpPresetTests(unittest.TestCase):
    def test_nothing_is_enabled_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(mcp_presets.preset_servers(), [])
            self.assertEqual(mcp_auto_discover.discover_mcp_servers_from_env(), [])

    def test_selected_presets_ignore_unknown_names(self):
        self.assertEqual(mcp_presets.selected_presets("Blender, nope ,context-mode,blender"),
                         ["blender", "context-mode"])

    def test_blender_is_safe_by_default_but_the_host_env_wins(self):
        with mock.patch.dict(os.environ, {"AURIX_MCP_PRESETS": "blender"}, clear=True):
            (srv,) = mcp_auto_discover.discover_mcp_servers_from_env()
        self.assertEqual(srv["id"], "builtin_preset_blender")
        self.assertEqual((srv["command"], srv["args"]), ("uvx", ["mcp-for-blender"]))
        self.assertEqual(srv["env"]["BLENDER_MCP_SAFE_MODE"], "1")
        with mock.patch.dict(os.environ, {"AURIX_MCP_PRESETS": "blender", "BLENDER_MCP_SAFE_MODE": "0"}, clear=True):
            (srv,) = mcp_presets.preset_servers()
        self.assertEqual(srv["env"]["BLENDER_MCP_SAFE_MODE"], "0")

    def test_env_servers_get_builtin_ids_and_url_means_sse(self):
        env = {"MCP_SERVERS": '[{"name": "My Tools", "url": "https://x.example/sse"}]',
               "AURIX_MCP_PRESETS": "context-mode"}
        with mock.patch.dict(os.environ, env, clear=True):
            servers = mcp_auto_discover.discover_mcp_servers_from_env()
        self.assertEqual([s["id"] for s in servers], ["builtin_env_my_tools", "builtin_preset_context_mode"])
        self.assertEqual(servers[0]["transport"], "sse")


class SkillAndPackTests(unittest.TestCase):
    def test_new_skills_fall_back_to_embedded_rules(self):
        with tempfile.TemporaryDirectory() as empty:
            self.assertIn("Verify before delivering", skills.load("archify", roots=[Path(empty)]))
            self.assertIn("Answer-first", skills.load("adhd", roots=[Path(empty)]))

    def test_diagram_goals_get_the_archify_pack(self):
        packs = capabilities.match_packs("draw a sequence diagram of the login flow")
        self.assertIn("diagrams", [p.id for p in packs])
        self.assertEqual(next(p for p in packs if p.id == "diagrams").skills, ("archify",))

    def test_optional_tools_are_registered_but_required_by_no_pack(self):
        for cap in ("hyperframes", "open-code-review"):
            self.assertIsNotNone(capabilities.lookup(cap))
            for p in capabilities.PACKS:
                required = set(p.capabilities) | {c for s in p.steps for c in s.capabilities}
                self.assertNotIn(cap, required)


class ExtrasLinksTests(unittest.TestCase):
    def test_links_only_when_the_extras_profile_is_on(self):
        from routes import command_routes
        req = mock.Mock()
        req.url.hostname, req.url.scheme = "100.1.2.3", "http"
        with mock.patch.dict(os.environ, {"COMPOSE_PROFILES": ""}, clear=False):
            self.assertEqual(command_routes._extras_links(req), {})
        with mock.patch.dict(os.environ, {"COMPOSE_PROFILES": "gpu,extras", "RECLIP_PORT": "9000"}, clear=False):
            links = command_routes._extras_links(req)
        self.assertEqual(links["reclip"], "http://100.1.2.3:9000/")
        self.assertTrue(links["files"].startswith("http://100.1.2.3:"))


if __name__ == "__main__":
    unittest.main()
