"""The Worker Registry & Router (src/foundation/workers.py). Offline: endpoints and the HTTP poster are fakes; audit goes to a temp root."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.foundation import audit, workers  # noqa: E402

# The live shape of AURIX's two endpoints on 2026-09-29, plus a cloud endpoint that is NOT authorised.
ENDPOINTS = [
    {"id": "c2375ceb", "name": "3431 GPU (Windows PC @ 100.64.0.10)", "base_url": "http://100.64.0.10:11434/v1",
     "is_enabled": True, "model_type": "llm",
     "cached_models": json.dumps(["qwen2.5-coder:7b", "hermes3:8b", "qwen2.5:7b", "qwen2.5:3b"]), "hidden_models": None},
    {"id": "local-d07e38d5", "name": "7070 local (CPU fallback)", "base_url": "http://host.docker.internal:11434/v1",
     "is_enabled": True, "model_type": "llm",
     "cached_models": json.dumps(["nomic-embed-text:latest", "qwen2.5:1.5b", "qwen2.5:3b", "llama3:latest"]), "hidden_models": None},
    {"id": "cloudy", "name": "Some Cloud", "base_url": "https://api.example.com/v1", "is_enabled": True, "model_type": "llm",
     "cached_models": json.dumps(["big-model-70b"]), "hidden_models": None},
]


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name})
        self.env.start()
        audit._heads.clear()
        self.ws = workers.registry(ENDPOINTS, authorizations={})

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def w(self, model):
        return next(x for x in self.ws if x.model == model)


class Registry(_Base):
    def test_locality_is_derived_and_unknown_hosts_are_cloud(self):
        self.assertEqual(workers.locality_of("http://100.64.0.10:11434/v1"), "lan")        # tailnet
        self.assertEqual(workers.locality_of("http://10.0.0.46:11434"), "lan")
        self.assertEqual(workers.locality_of("http://host.docker.internal:11434/v1"), "local")
        self.assertEqual(workers.locality_of("https://api.example.com/v1"), "cloud")
        self.assertEqual(workers.locality_of("http://some-unknown-host:11434"), "cloud")        # fails closed

    def test_models_become_workers_with_honest_traits(self):
        coder = self.w("qwen2.5-coder:7b")
        self.assertEqual((coder.locality, coder.coder, coder.params_b, coder.backend), ("lan", True, 7.0, "ollama"))
        self.assertTrue(self.w("llama3:latest").cpu_only)
        # UPDATED 2026-09-30: embedding (and image/speech/video) models are not text workers, so they are not registered at all
        self.assertFalse(any(x.model == "nomic-embed-text:latest" for x in self.ws))

    def test_cloud_workers_are_off_until_the_owner_authorizes_that_worker(self):
        cloud = self.w("big-model-70b")
        self.assertFalse(cloud.enabled)
        self.assertIn("authorize worker-some-cloud", cloud.note)
        granted = workers.registry(ENDPOINTS, authorizations={"worker-some-cloud": {}})
        self.assertTrue(next(x for x in granted if x.model == "big-model-70b").enabled)

    def test_executors_are_listed_but_never_callable_by_ask(self):
        ex = [x for x in self.ws if x.backend == "executor"]
        self.assertEqual({x.id for x in ex}, {"openhands", "openclaw"})
        self.assertTrue(all(not x.purposes for x in ex))

    def test_hidden_and_non_llm_endpoints_are_skipped(self):
        eps = [dict(ENDPOINTS[0], hidden_models=json.dumps(["hermes3:8b"])), dict(ENDPOINTS[1], model_type="image")]
        models = {x.model for x in workers.registry(eps, authorizations={})}
        self.assertNotIn("hermes3:8b", models)
        self.assertNotIn("llama3:latest", models)


class Routing(_Base):
    def test_code_edits_go_to_the_coder_model_on_the_gpu(self):
        self.assertEqual(workers.route("code_edit", "private", self.ws)[0].model, "qwen2.5-coder:7b")

    def test_planning_and_writing_go_to_the_strongest_general_model(self):
        for p in ("plan", "review", "write"):
            self.assertEqual(workers.route(p, "private", self.ws)[0].model, "hermes3:8b", p)

    def test_the_cpu_never_gets_heavy_work(self):
        for p in ("code_edit", "code", "plan", "review", "write", "general"):
            self.assertFalse(any(x.cpu_only for x in workers.route(p, "private", self.ws)), p)

    def test_the_cpu_can_take_light_work_but_only_after_the_gpu(self):
        r = workers.route("classify", "private", self.ws)
        self.assertFalse(r[0].cpu_only)
        self.assertTrue(any(x.cpu_only for x in r))

    def test_private_data_never_routes_to_the_cloud_even_if_authorized(self):
        granted = workers.registry(ENDPOINTS, authorizations={"worker-some-cloud": {}})
        for dc in ("private", "internal"):
            self.assertFalse(any(x.locality == "cloud" for x in workers.route("plan", dc, granted)), dc)
        self.assertTrue(any(x.locality == "cloud" for x in workers.route("plan", "public", granted)))

    def test_unknown_data_class_or_purpose_fails_closed(self):
        granted = workers.registry(ENDPOINTS, authorizations={"worker-some-cloud": {}})
        self.assertFalse(any(x.locality == "cloud" for x in workers.route("plan", "whatever", granted)))
        self.assertTrue(workers.route("made-up-purpose", "private", self.ws))              # treated as general, not refused


class Ask(_Base):
    def test_code_edits_ask_for_enforced_json_from_the_coder(self):
        seen = []

        def post(url, body, timeout):
            seen.append((url, body))
            return 200, {"message": {"content": '{"edits": []}'}}
        text, info = workers.ask("code_edit", "SYS", "PROMPT", workers=self.ws, post=post)
        self.assertEqual(text, '{"edits": []}')
        url, body = seen[0]
        self.assertEqual(url, "http://100.64.0.10:11434/api/chat")
        self.assertEqual((body["model"], body["format"]), ("qwen2.5-coder:7b", "json"))
        self.assertEqual((info["worker"].split("/")[-1], info["json"]), ("qwen2.5-coder:7b", True))

    def test_it_falls_through_to_the_next_worker_and_says_so(self):
        def post(url, body, timeout):
            if body["model"] == "hermes3:8b":
                raise TimeoutError("GPU box asleep")
            return 200, {"message": {"content": "a plan"}}
        text, info = workers.ask("plan", "SYS", "P", workers=self.ws, post=post)
        self.assertEqual(text, "a plan")
        self.assertIn("TimeoutError", info["tried"][0])

    def test_total_failure_is_reported_never_faked(self):
        text, info = workers.ask("plan", "S", "P", workers=self.ws, post=lambda u, b, t: (500, {"error": "boom"}))
        self.assertIsNone(text)
        self.assertIn("every allowed worker failed", info["reason"])

    def test_nothing_allowed_is_an_honest_refusal(self):
        only_cloud = workers.registry([ENDPOINTS[2]], authorizations={"worker-some-cloud": {}})
        text, info = workers.ask("plan", "S", "secret notes", data_class="private", workers=only_cloud,
                                 post=lambda u, b, t: self.fail("must not be called"))
        self.assertIsNone(text)
        self.assertIn("no enabled worker", info["reason"])

    def test_every_call_is_audited_without_content(self):
        workers.ask("summarize", "SYSTEM-SECRET", "PROMPT-SECRET", workers=self.ws,
                    post=lambda u, b, t: (200, {"message": {"content": "REPLY-SECRET"}}))
        rec = audit.recent(1)[0]
        self.assertEqual((rec["event"], rec["purpose"], rec["data_class"], rec["ok"]), ("worker_call", "summarize", "private", True))
        blob = json.dumps(rec)
        for secret in ("SYSTEM-SECRET", "PROMPT-SECRET", "REPLY-SECRET"):
            self.assertNotIn(secret, blob)

    def test_the_owner_view_is_honest_about_what_exists(self):
        text = workers.status_text(self.ws)
        self.assertIn("qwen2.5-coder:7b", text)
        self.assertIn("code_edit → qwen2.5-coder:7b", text)
        self.assertIn("⛔", text)                                                         # the unauthorised cloud worker
        self.assertIn("❌ No model workers", workers.status_text([]))


if __name__ == "__main__":
    unittest.main()


class LanesUseTheRouter(_Base):
    """Every free-local lane goes through the router with its purpose (2026-09-29)."""

    def test_local_fallback_routes_by_purpose_and_json(self):
        from src.foundation import teacher
        with mock.patch.object(workers, "ask", return_value=("{}", {"worker": "w"})) as ask:
            self.assertEqual(teacher._local_fallback("S", "U", 99, purpose="code_edit", json_mode=True), ("{}", ""))
        args, kw = ask.call_args
        self.assertEqual((args[0], kw["json_mode"], kw["data_class"], kw["max_tokens"]), ("code_edit", True, "private", 99))

    def test_real_worker_failures_are_reported_not_masked_by_the_legacy_path(self):
        from src.foundation import teacher
        info = {"worker": None, "tried": ["x: HTTP 500"], "reason": "every allowed worker failed: x: HTTP 500"}
        with mock.patch.object(workers, "ask", return_value=(None, info)):
            text, err = teacher._local_fallback("S", "U")
        self.assertIsNone(text)
        self.assertIn("every allowed worker failed", err)

    def test_each_lane_asks_for_its_purpose(self):
        import functools
        from src.foundation import content, freelance, learning, teacher, upgrades
        seen = {}

        def fake(system, text, max_tokens, purpose="general", json_mode=None, data_class="private"):
            seen[purpose] = json_mode
            return None, "stop here"
        with mock.patch.object(teacher, "_local_fallback", fake), mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            upgrades._call_engineer("do a thing", post=lambda *a: (500, {}))
            freelance._call_drafter("write a script", post=lambda *a: (500, {}))
            teacher.call_teacher("teach", post=lambda *a: (500, {}))
        self.assertEqual(seen.get("code_edit"), True)
        self.assertEqual(seen.get("code"), True)
        self.assertEqual(seen.get("review"), True)


class Wiring(_Base):
    def test_command_button_and_protection(self):
        from src.foundation import buttons, commands, identity
        self.assertEqual(commands.parse("models"), ("workers", ""))
        self.assertEqual(commands.parse("router"), ("workers", ""))
        self.assertEqual(commands.parse("workers"), ("shards", ""))                       # unchanged: "workers" means shards
        self.assertEqual(commands.parse("who does what?"), ("workers", ""))
        self.assertIn("workers", buttons.ALLOWED_KINDS)
        self.assertEqual(identity.protected_component_for("src/foundation/workers.py"), "approval_gate")


CLOUD = [{"id": "gem", "name": "Gemini free", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "is_enabled": True,
          "model_type": "llm", "cached_models": json.dumps(["gemini-2.5-flash"]), "hidden_models": None}]


class FreeFrontier(_Base):
    """Free-tier cloud workers (2026-09-29): authorised per worker, never for private data, internal only with `cloud-internal`."""

    def setUp(self):
        super().setUp()
        self.cws = workers.registry(ENDPOINTS[:1] + CLOUD, authorizations={"worker-gemini-free": {}})

    def test_cloud_goes_first_for_heavy_internal_work_only_when_authorized(self):
        self.assertFalse(any(w.locality == "cloud" for w in workers.route("code_edit", "internal", self.cws, cloud_internal=False)))
        r = workers.route("code_edit", "internal", self.cws, cloud_internal=True)
        self.assertEqual(r[0].model, "gemini-2.5-flash")                                    # frontier first
        self.assertEqual(r[1].model, "qwen2.5-coder:7b")                                    # local stays as the fallback

    def test_private_never_reaches_the_cloud_even_with_every_authorization(self):
        self.assertFalse(any(w.locality == "cloud" for w in workers.route("plan", "private", self.cws, cloud_internal=True)))

    def test_cloud_call_uses_the_resolver_and_json_mode_and_falls_back_locally(self):
        seen = []

        def cloud_post(url, headers, body, timeout):
            seen.append((url, headers, body))
            return 429, {"error": {"message": "rate limited"}}
        text, info = workers.ask("code_edit", "S", "P", data_class="internal", workers=self.cws, cloud_internal=True,
                                 resolve=lambda w: ("https://x/chat/completions", {"Authorization": "Bearer SECRET-KEY"}),
                                 cloud_post=cloud_post, post=lambda u, b, t: (200, {"message": {"content": '{"ok": 1}'}}))
        self.assertEqual(text, '{"ok": 1}')                                                 # the local coder took over
        self.assertEqual(seen[0][2]["response_format"], {"type": "json_object"})
        self.assertIn("HTTP 429", info["tried"][0])
        self.assertNotIn("SECRET-KEY", json.dumps(audit.recent(5)))                          # the key never reaches the audit log

    def test_a_400_on_json_mode_is_retried_once_without_it(self):
        calls = []

        def cloud_post(url, headers, body, timeout):
            calls.append("response_format" in body)
            return (400, {}) if body.get("response_format") else (200, {"choices": [{"message": {"content": "{}"}}]})
        text, info = workers.ask("code_edit", "S", "P", data_class="internal", workers=self.cws, cloud_internal=True,
                                 resolve=lambda w: ("u", {}), cloud_post=cloud_post, post=lambda u, b, t: self.fail("local not needed"))
        self.assertEqual((text, calls), ("{}", [True, False]))

    def test_the_daily_free_tier_budget_is_respected(self):
        wid = next(w.id for w in self.cws if w.locality == "cloud")
        for _ in range(workers.CLOUD_DAILY_CALLS):
            workers._count_call(wid)
        text, info = workers.ask("plan", "S", "P", data_class="internal", workers=self.cws, cloud_internal=True,
                                 resolve=lambda w: self.fail("budget exhausted: must not call"),
                                 post=lambda u, b, t: (200, {"message": {"content": "local plan"}}))
        self.assertEqual(text, "local plan")
        self.assertIn("daily budget", info["tried"][0])

    def test_lanes_declare_their_data_class(self):
        from src.foundation import teacher, upgrades
        seen = {}

        def fake(system, text, max_tokens, purpose="general", json_mode=None, data_class="private"):
            seen[purpose] = data_class
            return None, "stop"
        with mock.patch.object(teacher, "_local_fallback", fake), mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            upgrades._call_engineer("x", post=lambda *a: (500, {}))
        self.assertEqual(seen["code_edit"], "internal")
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "foundation", "session_rotation.py")).read()
        self.assertIn('data_class="private"', src)                                          # chat logs never leave


class OwnerAuthorizesCloud(_Base):
    def test_authorize_accepts_only_real_cloud_workers_and_cloud_internal(self):
        from src.foundation import capabilities, commands
        f = commands.Foundation(lambda x: None, lambda x: None, llm=None, session_id="tg")
        real = workers.registry
        with mock.patch.object(workers, "_load_endpoints", return_value=ENDPOINTS):          # the owner's endpoints table
            self.assertIn("not one of your cloud model endpoints", f.authorize("worker-made-up"))
            self.assertIn("enabled for PUBLIC work", f.authorize("worker-some-cloud"))
        self.assertIn("worker-some-cloud", capabilities.load_authorizations())
        self.assertTrue(next(w for w in real(ENDPOINTS) if w.model == "big-model-70b").enabled)
        f.authorize("cloud-internal")
        self.assertIn("cloud-internal", capabilities.load_authorizations())
        self.assertTrue(workers._authorized("cloud-internal", None))
        self.assertIn("revoked", f.revoke("worker-some-cloud"))



class GeminiCatalogue(_Base):
    """2026-09-30: the real Gemini endpoint listed 61 ids incl. image/music/video/speech/robotics models."""
    IDS = ["models/gemini-2.5-flash", "models/gemini-2.5-pro", "models/gemini-2.5-flash-preview-tts", "models/gemini-pro-latest",
           "models/gemini-flash-latest", "models/gemini-3.1-flash-lite", "models/veo-3.1-generate-preview", "models/lyria-3.5",
           "models/gemini-embedding-001", "models/gemini-3-pro-image", "models/gemini-robotics-er-2-preview", "models/aqa",
           "models/deep-research-preview-04-2026", "models/gemini-3.1-pro-preview", "models/gemini-2.5-flash-native-audio-latest"]

    def setUp(self):
        super().setUp()
        ep = {"id": "g", "name": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
              "is_enabled": True, "model_type": "llm", "cached_models": json.dumps(self.IDS), "hidden_models": None}
        self.gws = workers.registry([ep], authorizations={"worker-google-gemini": {}})

    def test_only_chat_models_become_workers(self):
        self.assertEqual(sorted(w.model for w in self.gws if w.locality == "cloud"),
                         sorted(["models/gemini-2.5-flash", "models/gemini-2.5-pro", "models/gemini-pro-latest", "models/gemini-flash-latest",
                                 "models/gemini-3.1-flash-lite", "models/gemini-3.1-pro-preview"]))

    def test_heavy_work_prefers_stable_pro_and_light_work_prefers_lite(self):
        self.assertEqual(workers.route("code_edit", "internal", self.gws, cloud_internal=True)[0].model, "models/gemini-pro-latest")
        self.assertEqual(workers.route("classify", "public", self.gws)[0].model, "models/gemini-3.1-flash-lite")

    def test_the_models_prefix_is_stripped_when_calling(self):
        seen = []
        workers.ask("plan", "S", "P", data_class="public", workers=self.gws, resolve=lambda w: ("u", {}),
                    cloud_post=lambda u, h, b, t: (seen.append(b["model"]), (200, {"choices": [{"message": {"content": "ok"}}]}))[1])
        self.assertEqual(seen[0], "gemini-pro-latest")


class Cooldown(_Base):
    def test_failed_workers_rest_and_are_skipped_honestly(self):
        ep = {"id": "g", "name": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "is_enabled": True,
              "model_type": "llm", "cached_models": json.dumps(["models/gemini-pro-latest", "models/gemini-flash-latest"]), "hidden_models": None}
        ws = workers.registry([ep], authorizations={"worker-google-gemini": {}})
        calls = []

        def cloud_post(url, headers, body, timeout):
            calls.append(body["model"])
            return (429, {}) if body["model"] == "gemini-pro-latest" else (200, {"choices": [{"message": {"content": "ok"}}]})
        kw = dict(data_class="public", workers=ws, resolve=lambda w: ("u", {}), cloud_post=cloud_post)
        workers.ask("plan", "S", "P", **kw)
        text, info = workers.ask("plan", "S", "P", **kw)
        self.assertEqual(text, "ok")
        self.assertEqual(calls, ["gemini-pro-latest", "gemini-flash-latest", "gemini-flash-latest"])   # Pro not retried
        self.assertIn("resting after a recent failure", info["tried"][0])

    def test_a_bad_request_does_not_bench_the_worker(self):
        workers._cool("w", "HTTP 400: bad", 1000.0)
        self.assertEqual(workers._cooled("w", 1001.0), 0.0)
        workers._cool("w", "HTTP 404: nope", 1000.0)
        self.assertGreater(workers._cooled("w", 1001.0), 1000.0)
