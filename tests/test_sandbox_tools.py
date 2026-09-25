"""Pure logic in the sandbox's baked-in tools (mission_sandbox/tools). The imaging/ASR/SDK parts need the
sandbox image (numpy, whisper, OpenHands) and are covered by `mission_sandbox/verify_tools.sh` on the host;
everything decidable without them is tested here, on the dev PC, with the standard library only."""
import contextlib
import importlib.util
import io
import json
import os
import sys
import unittest

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mission_sandbox", "tools")


def _load(name):
    spec = importlib.util.spec_from_file_location(f"sbxtool_{name}", os.path.join(TOOLS, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pc, tr, ct, oh = _load("print_cost"), _load("transcribe"), _load("ct_to_stl"), _load("openhands_run")

SPHERE = {"volume_cm3": 268.0, "area_cm2": 201.0, "extents_mm": [80, 80, 80], "watertight": True}
BIG = {"volume_cm3": 900.0, "area_cm2": 700.0, "extents_mm": [400, 300, 300], "watertight": True}


class PrintCostTests(unittest.TestCase):
    def test_builtin_selftest(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pc._selftest(), 0)

    def test_fits_is_orientation_independent(self):
        self.assertTrue(pc.fits([100, 200, 50], (250, 120, 60)))
        self.assertFalse(pc.fits([300, 10, 10], (250, 250, 250)))

    def test_scale_to_fit(self):
        self.assertEqual(pc.scale_to_fit([10, 10, 10], (100, 100, 100)), 1.0)
        self.assertAlmostEqual(pc.scale_to_fit([500, 100, 100], (250, 250, 250)), 0.5)

    def test_fdm_uses_walls_plus_infill_and_never_more_than_solid(self):
        self.assertLess(pc.fdm_effective_cm3(268, 201), 268)
        self.assertEqual(pc.fdm_effective_cm3(0.5, 500), 0.5)              # thin part: all wall, capped at volume
        self.assertGreater(pc.fdm_effective_cm3(268, 201, infill=1.0), pc.fdm_effective_cm3(268, 201, infill=0.1))

    def test_home_estimate_is_cheapest_for_a_skull_sized_part(self):
        ests = pc.estimate_all(SPHERE)
        self.assertEqual(ests[0]["tech"], "fdm_pla_home")
        self.assertTrue(all(e["low"] <= e["high"] for e in ests))
        self.assertGreater(ests[0]["grams"], 0)

    def test_oversize_part_sorted_last_and_noted(self):
        ests = pc.estimate_all(BIG)
        by = {e["tech"]: e for e in ests}
        self.assertFalse(by["resin_sla_service"]["fits"])
        self.assertTrue(by["resin_sla_service"]["notes"])
        self.assertLess([e["fits"] for e in ests].index(False), len(ests))
        fits_flags = [e["fits"] for e in ests]
        self.assertEqual(fits_flags, sorted(fits_flags, reverse=True))     # everything that fits comes first

    def test_scale_cubes_volume(self):
        full = pc.estimate(SPHERE, "resin_sla_service")
        half = pc.estimate(SPHERE, "resin_sla_service", scale=0.5)
        setup_lo = pc.PRICE_TABLE["resin_sla_service"]["setup"][0]
        self.assertAlmostEqual((half["low"] - setup_lo) * 8, full["low"] - setup_lo, places=0)

    def test_quote_validation(self):
        good = {"vendor": "A", "tech": "sls", "price": 50, "source": "https://x"}
        self.assertEqual(pc.validate_quote(good), [])
        self.assertIn("missing vendor", pc.validate_quote({**good, "vendor": ""}))
        self.assertIn("negative price", pc.validate_quote({**good, "price": -1}))
        self.assertIn("price is not a number", pc.validate_quote({**good, "price": "cheap"}))
        self.assertTrue(any("no source" in p for p in pc.validate_quote({k: v for k, v in good.items() if k != "source"})))

    def test_ranking_orders_by_landed_cost_and_rejects_bad_rows(self):
        q = [{"vendor": "A", "tech": "sls_nylon_service", "price": 90, "shipping": 10, "source": "u"},
             {"vendor": "B", "tech": "sls_nylon_service", "price": 95, "shipping": 0, "source": "u"},
             {"vendor": "C", "tech": "sls_nylon_service", "price": 10},                        # no source
             {"vendor": "B", "tech": "sls_nylon_service", "price": 95, "source": "u2"}]        # duplicate
        ranked, rejected, _ = pc.rank_quotes(q, SPHERE)
        self.assertEqual([r["vendor"] for r in ranked], ["B", "A"])                            # 95 < 100 landed
        self.assertEqual(len(rejected), 2)
        self.assertTrue(any("duplicate" in "".join(r["problems"]) for r in rejected))

    def test_implausibly_cheap_quote_is_flagged_not_dropped(self):
        ranked, _, warnings = pc.rank_quotes(
            [{"vendor": "Scam", "tech": "sls_nylon_service", "price": 1, "source": "u"}], SPHERE)
        self.assertEqual(len(ranked), 1)
        self.assertIn("flag", ranked[0])
        self.assertTrue(warnings)

    def test_markdown_carries_disclaimer_and_rejections(self):
        ests = pc.estimate_all(SPHERE)
        ranked, rejected, warnings = pc.rank_quotes([{"vendor": "X", "tech": "sls_nylon_service", "price": -5}], SPHERE)
        md = pc.render_markdown(SPHERE, ests, ranked, rejected, warnings)
        self.assertIn("not quotes", md)
        self.assertIn("Rejected quote rows", md)
        self.assertIn("no usable quotes", md)

    def test_cli_roundtrip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            rp, out = os.path.join(d, "report.json"), os.path.join(d, "cost.md")
            json.dump(SPHERE, open(rp, "w"))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(pc.main(["--report", rp, "--out", out]), 0)
            self.assertIn("Print cost comparison", open(out, encoding="utf-8").read())


class TranscribeTests(unittest.TestCase):
    def test_timestamps(self):
        self.assertEqual(tr.fmt_srt_ts(3661.5), "01:01:01,500")
        self.assertEqual(tr.fmt_vtt_ts(0.0), "00:00:00.000")
        self.assertEqual(tr.fmt_srt_ts(-5), "00:00:00,000")                   # never negative
        self.assertEqual(tr.fmt_srt_ts(59.9996), "00:01:00,000")              # rounding carries correctly

    def test_glossary(self):
        rules = tr.parse_glossary("# names\nkubernetes cluster => Kubernetes cluster\nodyseus => Odysseus\nbad line\n => x\n")
        self.assertEqual(len(rules), 2)
        self.assertEqual(tr.apply_glossary("the odyseus app on my Kubernetes Cluster", tr.parse_glossary("odyseus => Odysseus")),
                         "the Odysseus app on my Kubernetes Cluster")
        self.assertEqual(tr.apply_glossary("nonodysseus", rules), "nonodysseus")   # whole words only
        self.assertEqual(tr.parse_glossary(None), [])

    def test_glossary_is_regex_safe(self):
        rules = tr.parse_glossary("c++ (old) => C++ (new)\nwin path => C:\\Users\\1")
        self.assertEqual(tr.apply_glossary("I use c++ (old) daily", rules), "I use C++ (new) daily")   # metachars in terms
        self.assertEqual(tr.apply_glossary("open win path now", rules), "open C:\\Users\\1 now")        # backslashes in the replacement

    def test_merge_short_segments(self):
        segs = [{"start": 0, "end": 1, "text": "Hi", "avg_logprob": -0.2, "no_speech_prob": 0.1},
                {"start": 1.2, "end": 3, "text": "there my friend, how are you", "avg_logprob": -0.9, "no_speech_prob": 0.3},
                {"start": 10, "end": 12, "text": "A long separate sentence follows here.", "avg_logprob": -0.1}]
        out = tr.merge_short_segments(segs)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["text"], "Hi there my friend, how are you")
        self.assertEqual((out[0]["start"], out[0]["end"]), (0, 3))
        self.assertEqual(out[0]["avg_logprob"], -0.9)                          # keeps the WORSE confidence
        self.assertEqual(out[0]["no_speech_prob"], 0.3)
        self.assertEqual(segs[0]["text"], "Hi")                                # input not mutated

    def test_low_confidence_flagged(self):
        flags = tr.flag_low_confidence([{"start": 0, "end": 1, "text": "ok", "avg_logprob": -0.1, "no_speech_prob": 0.0},
                                        {"start": 1, "end": 2, "text": "mumble", "avg_logprob": -1.5, "no_speech_prob": 0.9}])
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0]["index"], 2)
        self.assertEqual(len(flags[0]["reasons"]), 2)

    def test_writers(self):
        segs = [{"start": 0, "end": 1.5, "text": " Hello "}, {"start": 5, "end": 6, "text": "World"}]
        self.assertEqual(tr.to_srt(segs), "1\n00:00:00,000 --> 00:00:01,500\nHello\n\n2\n00:00:05,000 --> 00:00:06,000\nWorld\n\n")
        self.assertTrue(tr.to_vtt(segs).startswith("WEBVTT\n\n00:00:00.000 --> 00:00:01.500"))
        self.assertEqual(tr.to_txt(segs), "Hello\n\nWorld\n")                   # 3.5 s pause -> new paragraph
        self.assertEqual(tr.to_txt(segs, gap_para=10), "Hello World\n")

    def test_report_needs_spot_check_when_empty_or_flagged(self):
        self.assertTrue(tr.build_report("/x/a.mp4", 60, 30, "base", "en", [], [])["needs_spot_check"])
        seg = [{"start": 0, "end": 1, "text": "x"}]
        rep = tr.build_report("/x/a.mp4", 60, 30, "base", "en", seg, [])
        self.assertFalse(rep["needs_spot_check"])
        self.assertEqual((rep["source"], rep["realtime_factor"]), ("a.mp4", 0.5))
        self.assertTrue(tr.build_report("a", 60, 30, "base", "en", seg, [{"index": 1}])["needs_spot_check"])
        self.assertIsNone(tr.build_report("a", 0, 1, "base", "en", seg, [])["realtime_factor"])


class CtToStlTests(unittest.TestCase):
    def test_choose_series_prefers_most_slices_deterministically(self):
        self.assertEqual(ct.choose_series({"scout": 3, "head": 240, "neck": 240}), "head")
        with self.assertRaises(ValueError):
            ct.choose_series({})

    def test_keep_bodies_keeps_cavity_walls_and_drops_debris(self):
        # a hollow skull: outer surface, cranial-cavity wall (~78% as many faces), plus 3 specks of debris
        counts = [100_000, 78_000, 40, 12, 900]
        self.assertEqual(ct.keep_bodies(counts), [0, 1])              # the regression: the cavity used to be discarded
        self.assertEqual(ct.keep_bodies([500]), [0])
        self.assertEqual(ct.keep_bodies([500, 400, 4, 400]), [0, 1, 3])   # 4 < 1% of 500 -> debris; exactly 1% is kept
        self.assertEqual(ct.keep_bodies([1000, 10, 9], min_fraction=0.005), [0, 1, 2])
        with self.assertRaises(ValueError):
            ct.keep_bodies([])

    def test_hu_warnings(self):
        self.assertEqual(ct.hu_warnings(-1000, 3000, 300), [])
        self.assertTrue(any("bone threshold" in w for w in ct.hu_warnings(-1000, 100, 300)))
        self.assertTrue(any("air should be" in w for w in ct.hu_warnings(0, 3000, 300)))
        self.assertTrue(any("metal" in w for w in ct.hu_warnings(-1000, 30000, 300)))

    def test_spacing_warnings(self):
        self.assertEqual(ct.spacing_warnings((0.5, 0.5, 0.6)), [])
        self.assertTrue(any("thick slices" in w for w in ct.spacing_warnings((0.5, 0.5, 5.0))))
        self.assertTrue(any("anisotropic" in w for w in ct.spacing_warnings((0.3, 0.3, 1.2))))
        with self.assertRaises(ValueError):
            ct.spacing_warnings((0.5, 0, 1))

    def test_plan_resample(self):
        self.assertIsNone(ct.plan_resample((0.5, 0.5, 1.0), None))
        self.assertIsNone(ct.plan_resample((0.8, 0.8, 0.8), 0.8))
        self.assertEqual(ct.plan_resample((0.5, 0.5, 1.0), 0.8), (0.8, 0.8, 0.8))

    def test_face_budget(self):
        self.assertEqual(ct.face_budget(None), ct.DEFAULT_TARGET_FACES)
        self.assertEqual(ct.face_budget(0), ct.DEFAULT_TARGET_FACES)
        self.assertIsNone(ct.face_budget(-1))
        self.assertEqual(ct.face_budget(1000), 1000)

    def test_report_states_it_is_not_clinical_and_has_no_patient_fields(self):
        rep = ct.build_report({"series": "head"}, {"threshold": 300},
                              {"volume_cm3": 1.0, "area_cm2": 2.0, "extents_mm": [1, 2, 3], "watertight": True}, [], 1.234)
        self.assertIn("not for clinical", rep["purpose"])
        self.assertEqual(rep["elapsed_s"], 1.2)
        self.assertFalse({"patient", "patient_name", "patient_id", "birth_date"} & set(json.dumps(rep).lower().split('"')))

    def test_report_feeds_print_cost(self):
        rep = ct.build_report({}, {}, {"volume_cm3": 268.0, "area_cm2": 201.0, "extents_mm": [80, 80, 80], "watertight": True}, [], 1)
        self.assertEqual(pc.estimate_all(rep)[0]["tech"], "fdm_pla_home")        # the two tools agree on the report shape


class OpenHandsRunTests(unittest.TestCase):
    def test_emit_last_line_protocol(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = oh.emit("finished", "did\nthe   thing", 7)
        line = buf.getvalue().strip().splitlines()[-1]
        self.assertTrue(line.startswith(oh.RESULT_MARK))
        data = json.loads(line[len(oh.RESULT_MARK):])
        self.assertEqual((data["status"], data["summary"], data["iterations"], rc), ("finished", "did the thing", 7, 0))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(oh.emit("error", "x"), 3)

    def test_summary_is_length_capped(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            oh.emit("error", "x" * 5000)
        self.assertLessEqual(len(json.loads(buf.getvalue().strip()[len(oh.RESULT_MARK):])["summary"]), 1200)

    def test_normalize_model(self):
        self.assertEqual(oh.normalize_model("qwen2.5:14b"), "openai/qwen2.5:14b")
        self.assertEqual(oh.normalize_model("ollama/x"), "ollama/x")
        self.assertEqual(oh.normalize_model(""), "openai/qwen2.5:7b")

    def test_map_status(self):
        for raw, want in (("ExecutionStatus.FINISHED", "finished"), ("idle", "finished"), ("stuck", "stuck"),
                          ("ERROR", "error"), (None, "error"), ("max_iterations_reached", "max_iterations")):
            self.assertEqual(oh.map_status(raw), want, raw)

    def test_unreachable_llm_is_false_not_an_exception(self):
        self.assertFalse(oh.llm_reachable("http://127.0.0.1:9/v1", timeout=0.3))

    def test_reasoning_is_disabled_for_the_local_model(self):
        """Real failure seen live 2026-09-24: LLM's reasoning_effort defaults to "high", so LiteLLM asks the backend
        for extended thinking - which a small local model served over Ollama's OpenAI-compat endpoint rejects
        outright ("qwen2.5:7b does not support thinking"), failing every run before it could do anything."""
        from unittest import mock
        captured = {}

        class FakeLLM:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        class FakeConversation:
            def __init__(self, *a, **k):
                self.state = None

            def send_message(self, *a):
                pass

            def run(self):
                pass

        fake_sdk = (FakeLLM, mock.Mock(), FakeConversation, mock.Mock(name=""), mock.Mock(name=""), mock.Mock(name=""))
        pydantic_stub = mock.Mock()
        pydantic_stub.SecretStr = lambda v: v                                    # this dev PC has no real pydantic installed
        with mock.patch.object(oh, "load_sdk", return_value=fake_sdk), \
             mock.patch.object(oh, "llm_reachable", return_value=True), \
             mock.patch.dict(sys.modules, {"pydantic": pydantic_stub}), \
             mock.patch.dict(os.environ, {"LLM_BASE_URL": "http://x/v1", "LLM_MODEL": "qwen2.5:7b", "MISSION_WORKSPACE": "/tmp"}):
            import tempfile
            with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
                f.write("do the thing")
                task_path = f.name
            try:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    oh.run(task_path, 10)
            finally:
                os.unlink(task_path)
        self.assertEqual(captured.get("reasoning_effort"), "none")


class PackToolNoteTests(unittest.TestCase):
    """The packs tell the agent which baked-in tool to run; the notes must never drift from the tools."""

    def test_every_tool_and_flag_named_in_a_pack_note_exists(self):
        import re
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from src.foundation import capabilities as cap
        notes = [(p.id, st) for p in cap.PACKS for st in p.steps if st.sandbox_note]
        self.assertGreaterEqual(len(notes), 3)
        for pack_id, step in notes:
            tools = set(re.findall(r"/opt/tools/(\w+\.py)", step.sandbox_note))
            self.assertEqual(len(tools), 1, f"{pack_id}/{step.title}: a note should name exactly one tool, got {tools}")
            tool = tools.pop()
            path = os.path.join(TOOLS, tool)
            self.assertTrue(os.path.isfile(path), f"{pack_id}: {tool} is not in mission_sandbox/tools")
            src = open(path, encoding="utf-8").read()
            for flag in re.findall(r"(--[a-z][a-z-]+)", step.sandbox_note):
                self.assertIn(f'"{flag}"', src, f"{pack_id}: {tool} has no {flag} option")

    def test_notes_only_reach_sandboxed_missions(self):
        import asyncio, tempfile
        from unittest import mock
        from src.foundation import planner
        kw = dict(env={}, which=lambda b: None, find_spec=lambda m: None, probe=lambda h, p: False, authorizations={})
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": d}):
            box = asyncio.run(planner.propose_mission("convert my CT scan of a skull into an STL", sandboxed=True, **kw))
            bare = asyncio.run(planner.propose_mission("convert my CT scan of a skull into an STL", sandboxed=False, **kw))
        self.assertTrue(any("/opt/tools/ct_to_stl.py" in st.description for st in box.steps))
        self.assertFalse(any("/opt/tools/" in st.description for st in bare.steps))


if __name__ == "__main__":
    unittest.main()
