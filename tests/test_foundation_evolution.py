"""Tests for the evolution layer: growth.py (always-running panel, live thoughts, timeline, trust), announce.py (what's new + OK), experience.py
(failures -> permanent checks), actions.py (dashboard buttons)."""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import approval_gate as ag  # noqa: E402
from src.foundation import actions, announce, audit, buttons, commands, evals, experience, gaming, growth, mission as ms, teacher  # noqa: E402

KEY_HEX = "cd" * 32


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": str(t), "AURIX_BRAIN": str(t / "brain"), "AURIX_GAMING_DIR": str(t / "gaming"),
                                                 "AURIX_GAMING_HMAC_KEY": KEY_HEX, "ANTHROPIC_API_KEY": "", "AURIX_TIMEZONE": "America/Denver"})
        self.env.start()
        ag.reset_state()
        audit._heads.clear()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
        ag.reset_state()
        audit._heads.clear()


class ThoughtTests(_Base):
    def _log(self, lines):
        p = Path(os.environ["AURIX_BRAIN"]) / "logs"
        p.mkdir(parents=True)
        (p / "trade_agent.log").write_text("\n".join(lines), encoding="utf-8")

    def test_the_trading_agents_log_becomes_readable_thoughts(self):
        self._log(["2026-09-21 08:00:01 [TRADE] AAPL: BUY tech=0.61 conf=0.66 rsi=41 mom=1.2 | oversold bounce | Risk: earnings",
                   "2026-09-21 08:00:02 [TRADE] skip NVDA - tech score too low (0.41)",
                   "2026-09-21 08:00:03 [TRADE] PAPER BUY 84x AAPL @ $190.10 (quote $190.0 + slippage) conf=0.66",
                   "2026-09-21 08:00:04 [TRADE] scan complete (entries) - equity=$99,950.05 picks=1 core=300",
                   "some unrelated line", "2026-09-21 14:15:00 [TRADE] market closed or no data - nothing to do"])
        th = growth.trade_thoughts(10)
        texts = [t["text"] for t in th]
        self.assertTrue(texts[0].startswith("Considered AAPL: BUY"))
        self.assertIn("Passed on NVDA", texts[1])
        self.assertTrue(texts[2].startswith("Bought (paper):"))
        self.assertIn("scan complete", texts[3])
        self.assertIn("market closed", texts[4])
        self.assertEqual(len(th), 5)

    def test_missing_log_is_fine(self):
        self.assertEqual(growth.trade_thoughts(), [])

    def test_audit_events_become_sentences_and_noise_is_dropped(self):
        h = growth.humanize({"event": "mission_approved", "mission": "m-abc123", "by": "policy:fast_lane", "ts": "2026-09-20T15:41:43+0000"})
        self.assertIn("on its own (fast lane)", h["text"])
        self.assertEqual(growth.humanize({"event": "mission_proposed", "mission": "m-1", "objective": "x" * 200, "ts": "2026-09-20T15:41:06+0000"})["text"].count("x"), 69)
        self.assertIsNone(growth.humanize({"event": "some_internal_event"}))
        self.assertIn("v", growth.humanize({"event": "evals_run", "tiers": {"plan": "11/11"}, "ts": "2026-09-20T19:04:27+0000"})["text"].replace("plan 11/11", "v"))

    def test_feed_merges_sources_newest_first(self):
        audit.append("mission_completed", mission="m-abc123")
        self._log([f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [TRADE] PAPER BUY 1x SPY @ $500"])
        feed = growth.activity_feed(10)
        self.assertEqual({f["source"] for f in feed}, {"audit", "paper trading"})
        self.assertEqual([f["ts"] for f in feed], sorted([f["ts"] for f in feed], reverse=True))

    def test_next_scan_skips_weekends(self):
        tz = growth._tz()
        sat = datetime(2026, 9, 19, 12, 0, tzinfo=tz)
        self.assertTrue(growth.next_market_scan(sat).startswith("Mon"))
        mon_early = datetime(2026, 9, 21, 7, 0, tzinfo=tz)
        self.assertEqual(growth.next_market_scan(mon_early), "Mon 08:00")
        self.assertEqual(growth.next_daily(3, 15, datetime(2026, 9, 21, 1, 0, tzinfo=tz)), "today 03:15")
        self.assertEqual(growth.next_daily(3, 15, datetime(2026, 9, 21, 4, 0, tzinfo=tz)), "tomorrow 03:15")


class RunningPanelTests(_Base):
    def test_every_component_is_described_and_isolated(self):
        rows = growth.probe_running({"mission": None, "models": [], "in_flight": 0}, {"nervous": {"metrics": {"standing scheduler": "alive"}}})
        keys = [r["key"] for r in rows]
        self.assertEqual(keys, ["scheduler", "trading", "selfimprove", "gamepc", "runner", "model", "backup", "oldimprove"])
        for r in rows:
            self.assertEqual({"key", "title", "icon", "state", "what", "when", "thought"}, set(r))
            self.assertIn(r["state"], ("running", "idle", "asleep", "bad", "off"))
            self.assertTrue(r["what"] and r["thought"])

    def test_the_old_patch_agent_is_shown_honestly_as_asleep(self):
        p = Path(os.environ["AURIX_BRAIN"]) / "runtime"
        p.mkdir(parents=True)
        (p / "improvement_proposals.json").write_text(json.dumps([{"title": "fix the tui import"}]))
        old = [r for r in growth.probe_running({}, {}) if r["key"] == "oldimprove"][0]
        self.assertEqual(old["state"], "asleep")
        self.assertIn("fix the tui import", old["thought"])

    def test_the_self_improvement_loop_says_what_it_is_working_on(self):
        rows = {r["key"]: r for r in growth.probe_running({}, {})}
        self.assertIn("evals", rows["selfimprove"]["thought"])                                  # nothing run yet
        evals.record([{"id": "code-x", "tier": "code", "passed": False, "seconds": 0, "error": "x", "request": "r", "failure_class": "it raised the wrong error", "draft": {"code": "c"}}])
        rows = {r["key"]: r for r in growth.probe_running({}, {})}
        self.assertIn("code-x", rows["selfimprove"]["thought"])
        teacher.new_lesson({"title": "t", "diagnosis": "It skipped validation.", "guidance": ["g"], "applies_when": ["x"]}, "s", "m")
        rows = {r["key"]: r for r in growth.probe_running({}, {})}
        self.assertIn("Learning: It skipped validation.", rows["selfimprove"]["thought"])

    def test_the_mission_runner_shows_the_current_mission(self):
        r = [x for x in growth.probe_running({"mission": {"id": "m-abc123", "objective": "print primes"}}, {}) if x["key"] == "runner"][0]
        self.assertEqual(r["state"], "running")
        self.assertIn("print primes", r["thought"])

    def test_a_dead_scheduler_is_red(self):
        r = [x for x in growth.probe_running({}, {"nervous": {"metrics": {"standing scheduler": "not running"}}}) if x["key"] == "scheduler"][0]
        self.assertEqual(r["state"], "bad")


class TimelineTests(_Base):
    def test_milestones_load_and_are_well_formed(self):
        ms_ = growth.load_milestones()["milestones"]
        self.assertGreaterEqual(len(ms_), 8)
        for m in ms_:
            self.assertTrue(m["date"].startswith("2026-") and m["title"] and m["detail"])
        self.assertEqual([m["date"] for m in ms_], sorted(m["date"] for m in ms_))

    def test_timeline_merges_live_events_newest_first_and_marks_them(self):
        # a fixed, past-dated fixture: the live event below must win "newest" regardless of what real date
        # today happens to be or whether milestones.json has since grown an entry dated today
        fixed = {"now": {}, "milestones": [{"date": "2026-09-19", "icon": "x", "title": "old", "detail": "d"},
                                            {"date": "2026-09-20", "icon": "x", "title": "newer", "detail": "d"}]}
        with mock.patch.object(growth, "load_milestones", return_value=fixed):
            audit.append("mission_completed", mission="m-abc123")
            tl = growth.timeline(60)
        self.assertTrue(tl[0]["live"])
        self.assertIn("Finished mission m-abc123", tl[0]["title"])
        self.assertTrue(any(not it["live"] for it in tl))
        dates = [it["date"] for it in tl]
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_growth_stats_then_vs_now(self):
        evals.record([{"id": "a", "tier": "plan", "passed": False, "seconds": 0, "error": "x"}, {"id": "b", "tier": "plan", "passed": True, "seconds": 0, "error": ""}])
        evals.record([{"id": "a", "tier": "plan", "passed": True, "seconds": 0, "error": ""}, {"id": "b", "tier": "plan", "passed": True, "seconds": 0, "error": ""}])
        g = growth.growth_stats()
        self.assertEqual(g["evals_first"], {"plan": [1, 2]})
        self.assertEqual(g["evals_now"], {"plan": [2, 2]})
        self.assertGreater(g["commands"], 40)
        self.assertIn("days_alive", g)
        self.assertTrue(growth.growth_stats()["foundation_tests"])

    def test_text_versions(self):
        fixed = {"now": {}, "milestones": [{"date": "2026-09-20", "icon": "x", "title": "a milestone", "detail": "d"}]}
        with mock.patch.object(growth, "load_milestones", return_value=fixed):
            t = growth.timeline_text()
        self.assertIn("growth timeline", t)
        self.assertIn("2026-09-20", t)
        self.assertNotIn("<script", t)


class TrustTests(_Base):
    def test_trust_is_evidence(self):
        audit.append("auto_allowed", tool="bash")
        audit.append("approved", tool="bash")
        audit.append("denied", tool="bash", reason="protected_component")
        t = growth.trust()
        self.assertTrue(t["integrity_ok"])
        self.assertGreaterEqual(len(t["rails"]), 8)
        self.assertTrue(any("Real-money trading is refused" in r for r in t["rails"]))
        self.assertTrue(any("isolated sandbox" in r for r in t["rails"]))
        wk = t["week"]
        self.assertEqual(wk["did_on_its_own"], 1)
        self.assertEqual(wk["asked_and_you_approved"], 1)
        self.assertEqual(wk["refused_by_protection"], 1)

    def test_freelance_and_content_decisions_count_toward_the_weekly_track_record(self):
        """These emit their own event names (freelance_kept, content_discarded, ...), not the generic approved/denied -
        without this they would silently never show up as evidence in the trust panel."""
        audit.append("freelance_kept", id="f-abc123")
        audit.append("content_kept", id="c-abc123")
        audit.append("freelance_discarded", id="f-def456")
        audit.append("content_discarded", id="c-def456")
        wk = growth.trust()["week"]
        self.assertEqual(wk["asked_and_you_approved"], 2)
        self.assertEqual(wk["you_denied"], 2)

    def test_trust_text(self):
        text = growth.trust_text()
        self.assertIn("Why you can trust it", text)
        self.assertIn("verified intact", text)
        self.assertIn("Last 7 days", text)


class AnnounceTests(_Base):
    def test_the_first_run_is_a_silent_baseline_not_a_flood(self):
        self.assertEqual(announce.tick(), [])
        self.assertEqual(announce.tick(), [])

    def test_a_new_release_speaks_up_once_with_ok_and_timeline_buttons(self):
        announce.tick()                                                                             # baseline
        st = json.loads(announce._path().read_text())
        st["milestones"] = [m for m in st["milestones"] if not m.endswith(("|One push, a dashboard, and buttons", "|The game doctor"))]   # pretend these two are new
        announce._path().write_text(json.dumps(st))
        msgs = announce.tick()
        self.assertEqual(len(msgs), 1)
        self.assertIn("What's new with AURIX", msgs[0])
        self.assertIn("One push, a dashboard", msgs[0])
        self.assertIn("The game doctor", msgs[0])
        self.assertNotIn("The wall", msgs[0])
        kb = buttons.for_reply("announce", msgs[0])
        self.assertEqual([c for row in kb["inline_keyboard"] for c in [b["callback_data"] for b in row]], ["do:ack", "do:timeline"])
        self.assertEqual(announce.tick(), [])                                                       # only once

    def test_learning_a_skill_and_a_better_score_are_announced(self):
        evals.record([{"id": "a", "tier": "code", "passed": False, "seconds": 0, "error": "x"}, {"id": "b", "tier": "code", "passed": True, "seconds": 0, "error": ""}])
        announce.tick()
        audit.append("skill_forged", name="reverse_text")
        st = json.loads(announce._path().read_text())
        st["eval_ts"] = "2000-01-01T00:00:00+0000"                                                  # (the two runs land in the same second here; in life they are hours apart)
        announce._path().write_text(json.dumps(st))
        evals.record([{"id": "a", "tier": "code", "passed": True, "seconds": 0, "error": ""}, {"id": "b", "tier": "code", "passed": True, "seconds": 0, "error": ""}])
        [msg] = announce.tick()
        self.assertIn("reverse_text", msg)
        self.assertIn("code 1/2 → 2/2", msg)

    def test_ok_is_recorded_and_a_bare_ok_only_answers_a_waiting_announcement(self):
        announce.tick()
        self.assertIsNone(commands.parse("ok"))                                                     # nothing waiting: ordinary chat is never swallowed
        audit.append("skill_forged", name="tool_a")
        announce.tick()
        self.assertTrue(announce.pending_ack())
        self.assertEqual(commands.parse("okay!"), ("ack", ""))
        self.assertIn("Noted", announce.acknowledge())
        self.assertFalse(announce.pending_ack())
        self.assertIsNone(commands.parse("ok"))
        self.assertIn("owner_ack", json.dumps(audit.recent(5)))

    async def test_the_scheduler_tick_delivers_announcements_with_buttons(self):
        sent = []

        async def notify(text, kb=None):
            sent.append((text, kb))

        async def agent(p):
            return "ok"
        f = commands.Foundation(agent, notify, llm=None, session_id="tg", env={}, which=lambda b: None, find_spec=lambda m: None, probe=lambda h, p: False, authorizations={})
        await f.tick_standing(now=time.time())                                                      # baseline
        audit.append("skill_forged", name="tool_b")
        await f.tick_standing(now=time.time())
        news = [s for s in sent if "What's new" in s[0]]
        self.assertEqual(len(news), 1)
        self.assertIsNotNone(news[0][1])


class ExperienceTests(_Base):
    def _blocked_mission(self, objective="write a python script that prints the first 10 primes"):
        m = ms.MissionContract(id="m-abc123", objective=objective, steps=[ms.Step(id="s1", title="t")])
        ms.MissionStore().propose(m)
        audit.append("mission_blocked", mission="m-abc123", reason="preflight", missing=["alpaca-credentials (Alpaca paper-trading API key + secret)"])

    def test_classification_of_the_lessons_in_an_event(self):
        self.assertEqual(experience.classify({"event": "mission_blocked", "mission": "m-1", "reason": "preflight", "missing": ["alpaca-credentials (x)"]})["kind"], "failure")
        self.assertIn("alpaca-credentials", experience.classify({"event": "mission_blocked", "mission": "m-1", "reason": "preflight", "missing": ["alpaca-credentials (x)"]})["summary"])
        self.assertEqual(experience.classify({"event": "gaming_fix_undo_requested", "id": "g-abc123"})["kind"], "correction")
        self.assertEqual(experience.classify({"event": "gaming_fix_declined", "id": "g-abc123"})["kind"], "preference")
        self.assertEqual(experience.classify({"event": "denied", "tool": "bash", "reason": "protected_component"})["kind"], "preference")
        self.assertEqual(experience.classify({"event": "x", "reason": "protected_component"})["kind"], "guard")
        self.assertEqual(experience.classify({"event": "mission_completed", "mission": "m-1"})["kind"], "success")
        self.assertEqual(experience.classify({"event": "watchdog_alert", "preview": "✅ Cleared: <code>x</code>"}), {})
        self.assertEqual(experience.classify({"event": "digest_sent"}), {})

    def test_a_real_failure_becomes_a_permanent_self_check(self):
        self._blocked_mission()
        r = experience.harvest()
        self.assertEqual(r["learned"], 1)
        [task] = experience.learned_tasks()
        self.assertEqual(task["id"], "learned-m-abc123")
        self.assertEqual(task["expect"]["forbid_capabilities"], ["alpaca-credentials"])
        self.assertIn("first 10 primes", task["prompt"])
        self.assertIn("learned-m-abc123", [t["id"] for t in evals.load_tasks("plan")])               # it now sits in the exam
        self.assertEqual(experience.harvest(), {"new": 0, "learned": 0})                            # idempotent
        self.assertIn("learned_check_created", json.dumps(audit.recent(5)))

    def test_the_learned_check_passes_with_todays_planner_and_would_have_failed_the_old_one(self):
        self._blocked_mission()
        experience.harvest()
        task = [t for t in evals.load_tasks("plan") if t["id"] == "learned-m-abc123"][0]
        present = dict(env={}, which=lambda b: "/usr/bin/" + b, find_spec=lambda m: object(), authorizations={}, probe=lambda h, p: True)
        res = asyncio.run(evals.run_plan_tier([task], None, True, present))
        self.assertTrue(res[0]["passed"], res[0]["checks"])

        async def old_planner(goal, llm=None, session_id=None, sandboxed=False, **kw):                # a planner that repeats the bug
            from src.foundation import planner as p
            m = await p.draft_mission(goal, llm=None, session_id=session_id, sandboxed=sandboxed, **kw)
            m.steps[0].capabilities.append("alpaca-credentials")
            return m
        res = asyncio.run(evals.run_plan_tier([task], None, True, present, draft=old_planner))
        self.assertFalse(res[0]["passed"])

    def test_ledger_stats_and_usage(self):
        audit.append("gaming_fix_declined", id="g-abc123")
        audit.append("mission_completed", mission="m-abc123")
        experience.harvest()
        experience.count_usage("games")
        experience.count_usage("games")
        experience.count_usage("fixes")
        experience.count_usage("")
        st = experience.stats()
        self.assertEqual(st["experiences"], 2)
        self.assertEqual(st["by_kind"], {"preference": 1, "success": 1})
        self.assertEqual(st["top_used"][0], ("games", 2))
        self.assertNotIn("text", json.dumps(st["top_used"]))                                        # kinds only, never message text

    async def test_handle_counts_command_kinds_only(self):
        async def notify(t):
            pass

        async def agent(p):
            return "ok"
        f = commands.Foundation(agent, notify, llm=None, session_id="tg", env={}, which=lambda b: None, find_spec=lambda m: None, probe=lambda h, p: False, authorizations={})
        await f.handle("ping", "")
        await f.handle("timeline", "")
        used = dict(experience.stats()["top_used"])
        self.assertEqual(used.get("ping"), 1)
        self.assertEqual(used.get("timeline"), 1)


class PanelTests(_Base):
    def test_games_panel_orders_by_need_and_never_crashes_without_a_report(self):
        from src.foundation import panels
        self.assertEqual(panels.games()["rows"], [])
        d = Path(os.environ["AURIX_GAMING_DIR"])
        d.mkdir(parents=True)
        recent = (datetime.now().astimezone() - timedelta(days=1)).isoformat(timespec="seconds")
        (d / "report.json").write_text(json.dumps({"generated_ts": time.time() - 600, "libraries": [{"path": "C:\S", "free_gb": 400, "total_gb": 2000}], "games": [
            {"appid": "1", "name": "Zelda", "size_gb": 10, "known": True, "crash_events": [], "plugins": None},
            {"appid": "2", "name": "Skyrim", "size_gb": 80, "known": True, "crash_events": [{"when": recent, "module": "x.dll"}], "plugins": {"enabled": ["A.esp"], "masters": {}, "present": ["A.esp"], "light": [], "implicit": []}},
            {"appid": "3", "name": "Broken", "size_gb": 5, "known": True, "crash_events": [], "plugins": {"enabled": ["B.esp"], "masters": {"B.esp": ["Gone.esm"]}, "present": ["B.esp"], "light": [], "implicit": []}}]}))
        g = panels.games()
        self.assertEqual([(r["name"], r["state"]) for r in g["rows"]], [("Broken", "bad"), ("Skyrim", "warn"), ("Zelda", "ok")])
        self.assertEqual(g["rows"][1]["crashes"], 1)
        self.assertLess(g["age_h"], 1)
        self.assertEqual(g["libraries"][0]["free_gb"], 400)

    def test_trading_panel(self):
        from src.foundation import panels
        self.assertEqual(panels.trading(), {"have": False})
        rt = Path(os.environ["AURIX_BRAIN"]) / "runtime"
        rt.mkdir(parents=True)
        (rt / "trade_report.json").write_text(json.dumps({"lifeforce_pct": 101.2, "return_pct": 1.2, "spy_return_pct": 0.5, "excess_pct": 0.7, "drawdown_from_peak_pct": 0.3,
                                                          "dies_at_drawdown_pct": 35, "trades_closed": 2, "last_scan": "2026-09-21T08:00:00", "killed": False, "dead": False,
                                                          "open_positions": [{"symbol": "SPY", "qty": 1, "entry": 500, "core": True, "pnl_pct": 0.4}, {"symbol": "AAPL", "qty": 3, "entry": 190, "pnl_pct": -1.1}]}))
        t = panels.trading()
        self.assertTrue(t["have"])
        self.assertEqual((t["lifeforce_pct"], t["dies_at_pct"], t["last_scan"]), (101.2, 35, "2026-09-21 08:00"))
        self.assertEqual([(p["symbol"], p["core"]) for p in t["positions"]], [("SPY", True), ("AAPL", False)])

    def test_checks_and_lessons_panels(self):
        from src.foundation import panels
        self.assertEqual(panels.checks(), {"have": False})
        self.assertEqual(panels.lessons(), [])
        evals.record([{"id": "code-x", "tier": "code", "passed": False, "seconds": 0, "error": "x"}, {"id": "code-y", "tier": "code", "passed": True, "seconds": 0, "error": ""}])
        c = panels.checks()
        self.assertEqual((c["tiers"]["code"]["passed"], c["tiers"]["code"]["total"], c["tiers"]["code"]["failed"]), (1, 2, ["code-x"]))
        evals.record([{"id": "plan-a", "tier": "plan", "passed": True, "seconds": 0, "error": ""}])
        evals.record([{"id": "code-x", "tier": "code", "passed": True, "seconds": 0, "error": ""}])
        c = panels.checks()
        self.assertEqual((c["tiers"]["plan"]["passed"], c["tiers"]["code"]["passed"]), (1, 1))          # each tier keeps its own latest result
        teacher.new_lesson({"title": "Validate types", "diagnosis": "It skipped checks.", "guidance": ["g"], "applies_when": ["x"]}, "s", "m")
        self.assertEqual([(x["title"], x["status"]) for x in panels.lessons()], [("Validate types", "pending")])

    def test_the_snapshot_carries_every_panel(self):
        from src.foundation import command_center
        snap = command_center.snapshot()
        for k in ("games", "trading", "lessons", "checks", "alive", "growth", "timeline", "trust", "decisions"):
            self.assertIn(k, snap)


class DigestTests(_Base):
    def test_the_morning_digest_leads_with_what_waits_for_you_and_carries_the_taps(self):
        from src.foundation import heartbeat, memory
        self.assertIn("Nothing is waiting", heartbeat.waiting_for_you())
        gaming.save_proposal({"id": "g-abc123", "key": "k", "game": "G", "appid": "1", "kind": "missing_master", "severity": "high", "title": "3 plugins would crash",
                              "evidence": ["e"], "fix": {"actions": [], "summary": "s", "risk": "r"}, "status": "pending", "created": time.time()})
        memory.observe("I prefer short answers.")
        text = heartbeat.overnight_digest()
        self.assertIn("Waiting for your yes / no", text)
        self.assertLess(text.index("Waiting for your yes"), text.index("AURIX status"))
        self.assertIn("yes g-abc123", text)
        cmds = [b["callback_data"] for row in buttons.for_reply("digest", text)["inline_keyboard"] for b in row]
        self.assertIn("do:yes g-abc123", cmds)
        self.assertTrue(any(c.startswith("do:yes k-") for c in cmds))
        self.assertIn("do:all", cmds)


class ActionTests(_Base):
    def _fix(self):
        gaming.save_proposal({"id": "g-abc123", "key": "k", "game": "G", "appid": "1", "kind": "missing_master", "severity": "high", "title": "3 plugins would crash",
                              "evidence": ["A needs B"], "fix": {"actions": [{"type": "plugins_disable", "plugins": ["A.esp"]}], "summary": "Switch A off", "risk": "Low"},
                              "status": "pending", "created": time.time()})

    def test_game_fix_buttons_do_what_the_typed_commands_do(self):
        self._fix()
        r = actions.run_action("fix_yes", "g-abc123")
        self.assertTrue(r["ok"])
        self.assertIn("Approved", r["message"])
        self.assertEqual(gaming.load_proposal("g-abc123")["status"], "approved")
        self._fix()
        self.assertIn("Declined", actions.run_action("fix_no", "g-abc123")["message"])

    def test_arguments_are_validated_and_unknowns_refused(self):
        self.assertFalse(actions.run_action("fix_yes", "../../etc")["ok"])
        self.assertFalse(actions.run_action("fix_yes", "")["ok"])
        self.assertFalse(actions.run_action("approve_mission", "m-abc123")["ok"])
        self.assertFalse(actions.run_action("rm_rf", "")["ok"])
        self.assertEqual(actions.run_action("rm_rf", "")["message"], "unknown action")
        self.assertNotIn("approve_mission", actions.ACTION_NAMES)                                   # missions and credentials are not web buttons

    def test_every_allow_listed_action_is_actually_wired_up(self):
        """The route only lets ACTION_NAMES through; if one of them were ever added there without a handler here, the owner would just
        see 'unknown action' and have no idea it's a bug on this end, not a bad click. Confirm every current name IS wired, and that a
        drift like that is reported distinctly (and audited) rather than looking like ordinary user error."""
        try:
            with mock.patch.object(actions.threading, "Thread") as th:        # evals_plan/evals_all/teach start real background work otherwise
                th.return_value.start.side_effect = lambda: None
                for name in actions.ACTION_NAMES:
                    self.assertNotEqual(actions.run_action(name, "definitely-not-a-valid-id")["message"], "unknown action", name)
        finally:
            actions._BG.clear()
        with mock.patch.object(actions, "ACTION_NAMES", actions.ACTION_NAMES + ("not_really_wired",)):
            r = actions.run_action("not_really_wired")
        self.assertFalse(r["ok"])
        self.assertIn("bug on my end", r["message"])
        self.assertIn("action_not_wired", [x["event"] for x in audit.recent(5)])

    def test_switches(self):
        self.assertIn("OFF", actions.run_action("fastlane_off")["message"])
        self.assertIn("ON", actions.run_action("fastlane_on")["message"])
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-x"}):
            self.assertIn("ON", actions.run_action("teacher_on", "3")["message"])
            self.assertEqual(teacher.config()["daily_calls"], 3)
            self.assertIn("OFF", actions.run_action("teacher_off")["message"])
        self.assertIn("Asked your PC", actions.run_action("games_refresh")["message"])
        self.assertIn("Noted", actions.run_action("ack")["message"])

    def test_long_jobs_start_in_the_background_and_cannot_double_start(self):
        started = []
        with mock.patch.object(actions.threading, "Thread") as th:
            th.return_value.start.side_effect = lambda: started.append(1)
            r1 = actions.run_action("evals_plan")
            r2 = actions.run_action("evals_all")
        self.assertTrue(r1["ok"])
        self.assertFalse(r2["ok"])
        self.assertIn("already running", r2["message"])
        self.assertIn("evals", actions.background())
        actions._BG.clear()

    def test_teach_needs_the_teacher_to_be_on(self):
        r = actions.run_action("teach")
        self.assertFalse(r["ok"])
        self.assertIn("teacher", r["message"].lower())

    def test_decisions_lists_what_waits_for_you_with_buttons(self):
        self._fix()
        teacher.new_lesson({"title": "Validate types", "diagnosis": "It skipped checks.", "guidance": ["Check isinstance."], "applies_when": ["python"]}, "s", "m")
        d = actions.decisions()
        self.assertEqual([x["kind"] for x in d], ["game_fix", "lesson"])
        for card in d:
            self.assertEqual([b["style"] for b in card["buttons"]], ["approve", "deny"])
            self.assertTrue(all(b["action"] in actions.ACTION_NAMES for b in card["buttons"]))
        self.assertIn("Switch A off", d[0]["lines"])
        self.assertNotIn("<", json.dumps(d))                                                        # plain text only: the page uses textContent


class TileActionTests(_Base):
    def test_tiles_carry_real_buttons_that_map_to_allowed_actions(self):
        from src.foundation import addons
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-x"}):
            teacher.set_config(True, 5)
            tiles = {t["key"]: t for t in addons.probe_addons()}
        seen = [b["action"] for t in tiles.values() for b in t["actions"]]
        self.assertTrue({"games_refresh", "evals_plan", "evals_all", "teach", "teacher_off", "fastlane_off"} <= set(seen), seen)
        self.assertTrue(all(a in actions.ACTION_NAMES for a in seen))


if __name__ == "__main__":
    unittest.main()
