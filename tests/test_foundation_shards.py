"""Shards: the registry of everything autonomous, the pause switches (in the app AND in the host cron scripts), health lights, and the wiring."""
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from src import approval_gate as ag  # noqa: E402
from src.foundation import actions, audit, buttons, commands, fastlane, gamepilot, heartbeat, identity, memory, shards, teacher, upgrades  # noqa: E402


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, HERE / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


absorb = load("aurix_absorb_agent", "scripts/aurix_absorb_agent.py")
applier = load("aurix_upgrade_agent", "scripts/aurix_upgrade_agent.py")
sys.path.insert(0, str(HERE.parent / "tasks"))
try:
    import strategy_lab  # noqa: E402  tasks/strategy_lab.py: host-side script, may not be checked out
except ImportError:
    strategy_lab = None


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.sh = t / "shards"
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": str(t), "AURIX_BRAIN": str(t / "brain"), "AURIX_SHARDS_DIR": str(self.sh), "ANTHROPIC_API_KEY": "sk-ant-x",
                                                 "AURIX_UPGRADES_DIR": str(t / "up"), "AURIX_ABSORB_DIR": str(t / "ab"), "AURIX_GAMING_HMAC_KEY": "aa" * 32})
        self.env.start()
        ag.reset_state()
        audit._heads.clear()
        gamepilot.reset_memory()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
        ag.reset_state()
        audit._heads.clear()
        gamepilot.reset_memory()


class RegistryTests(_Base):
    def test_every_helper_is_described_with_what_it_spends_and_who_approves(self):
        self.assertEqual(len(shards.IDS), len(set(shards.IDS)))
        for r in shards.REGISTRY:
            self.assertEqual(len(r), 8)
            self.assertTrue(all(str(x).strip() for x in r), r[0])
        rows = shards.rows()
        self.assertEqual([r["id"] for r in rows], shards.IDS)
        for r in rows:
            self.assertTrue({"id", "name", "what", "schedule", "where", "spend", "gate", "pausable", "state", "detail"} <= set(r))
        self.assertFalse([r for r in rows if r["id"] in shards.PAUSABLE_NOT and r["pausable"]])
        self.assertIn("Anthropic", [r for r in rows if r["id"] == "upgrades"][0]["spend"])

    def test_pause_and_resume_one_and_all(self):
        self.assertIn("Paused", shards.set_paused("upgrades", True))
        self.assertEqual(shards.paused_ids(), ["upgrades"])
        self.assertTrue(shards.is_paused("upgrades"))
        self.assertIn("Resumed", shards.set_paused("upgrades", False))
        self.assertEqual(shards.paused_ids(), [])
        self.assertIn("Paused every helper", shards.set_paused("all", True))
        self.assertEqual(set(shards.paused_ids()), set(shards.IDS) - shards.PAUSABLE_NOT)
        self.assertIn("running again", shards.set_paused("all", False))
        self.assertEqual(shards.paused_ids(), [])
        self.assertIn("do not have a helper", shards.set_paused("nope", True))
        self.assertIn("outside my reach", shards.set_paused("game_doctor", True))
        self.assertIn("outside my reach", shards.set_paused("backup", True))
        self.assertEqual(shards.paused_ids(), [])
        events = [e["event"] for e in audit.recent(10)]
        self.assertEqual([e for e in events if e.startswith("shard")], ["shard_paused", "shard_resumed", "shards_pause_all", "shards_resume_all"])

    def test_a_corrupt_or_foreign_flag_file_is_harmless(self):
        self.sh.mkdir(parents=True)
        (self.sh / "paused.json").write_text("{not json")
        self.assertEqual(shards.paused_ids(), [])
        (self.sh / "paused.json").write_text(json.dumps(["upgrades", "evil", 5]))
        self.assertEqual(shards.paused_ids(), ["upgrades"])


class PauseReachesEveryHelperTests(_Base):
    def pause(self, sid):
        shards.set_paused(sid, True)

    def test_the_upgrade_lane_and_the_teacher(self):
        upgrades.set_config(True, 3)
        teacher.set_config(True, 5)
        self.assertTrue(upgrades.can_draft()[0])
        self.assertTrue(teacher.can_call()[0])
        self.pause("upgrades")
        self.pause("teacher")
        self.assertIn("paused", upgrades.can_draft()[1])
        self.assertIn("paused", teacher.can_call()[1])
        shards.set_paused("all", False)
        self.assertTrue(upgrades.can_draft()[0])

    def test_fast_lane_memory_intake_and_gamepilot(self):
        self.assertTrue(fastlane.enabled())
        self.pause("fastlane")
        self.assertFalse(fastlane.enabled())
        self.assertEqual(memory.observe("I prefer short answers."), 1)
        self.pause("memory")
        self.assertEqual(memory.observe("I hate loud notifications."), 0)
        tok = None
        r = gamepilot.new_pairing()
        gamepilot.confirm_pairing(r["code"])
        tok = gamepilot.pair_status(r["poll"])["token"]
        gamepilot.arm(5)
        self.assertGreater(gamepilot.armed_until(), 0)
        self.pause("gamepilot")
        self.assertEqual(gamepilot.armed_until(), 0.0)
        self.assertFalse(gamepilot.agent_poll({})["want_frames"])
        self.assertEqual(gamepilot.submit_input(tok, [{"t": "key", "k": "w"}])["error"], "not armed")

    def test_the_cron_scripts_on_the_host_honour_the_same_file(self):
        with mock.patch.object(absorb, "process_requests", side_effect=AssertionError("ran")), mock.patch.object(applier, "process_approved", side_effect=AssertionError("ran")):
            self.pause("absorb")
            self.pause("upgrade_applier")
            absorb.cycle()
            applier.cycle()                                                              # both return without doing anything
        shards.set_paused("all", False)
        calls = []
        with mock.patch.object(absorb, "process_requests", lambda: calls.append("a") or []), mock.patch.object(absorb, "process_approved", lambda k: []), mock.patch.object(absorb, "clean_old", lambda: None):
            absorb.cycle()
        self.assertEqual(calls, ["a"])

    @unittest.skipIf(strategy_lab is None, "tasks/strategy_lab.py is only on the host until it is committed")
    def test_the_strategy_lab_and_paper_trader_scripts_skip_when_paused(self):
        with mock.patch.object(strategy_lab, "fetch_history", side_effect=AssertionError("fetched")):
            self.pause("strategy_lab")
            self.assertEqual(strategy_lab.main(["--run"]), 0)                            # paused: returns before fetching anything
        src = (HERE.parent / "tasks" / "trade_agent.py").read_text(encoding="utf-8")
        self.assertIn('"paper_trading" in json.loads(_pf.read_text', src)
        self.assertIn('BRAIN / "sandbox" / "shards"', src)


class HealthTests(_Base):
    def test_states(self):
        by = {r["id"]: r for r in shards.rows()}
        self.assertEqual(by["upgrades"]["state"], "off")
        self.assertEqual(by["teacher"]["state"], "off")
        self.assertEqual(by["fastlane"]["state"], "ok")
        self.assertEqual(by["absorb"]["state"], "ok")
        upgrades.set_config(True, 3)
        (upgrades._data()).mkdir(parents=True, exist_ok=True)
        (upgrades._data() / "state.json").write_text(json.dumps({"last_attempt": time.time() - 20 * 3600}))
        self.assertEqual({r["id"]: r for r in shards.rows()}["upgrades"]["state"], "stale")
        (upgrades._data() / "state.json").write_text(json.dumps({"last_attempt": time.time() - 3600, "api_note_day": time.strftime("%Y-%m-%d")}))
        blocked = {r["id"]: r for r in shards.rows()}["upgrades"]
        self.assertEqual(blocked["state"], "blocked")
        self.assertIn("cannot reach the model", blocked["detail"])
        shards.set_paused("upgrades", True)
        self.assertEqual({r["id"]: r for r in shards.rows()}["upgrades"]["state"], "paused")

    def test_a_lane_whose_last_call_failed_at_the_api_is_flagged_even_on_a_new_day(self):
        upgrades.set_config(True, 3)
        (upgrades._data()).mkdir(parents=True, exist_ok=True)
        (upgrades._data() / "state.json").write_text(json.dumps({"last_attempt": time.time() - 3600, "api_note_day": "1999-01-01"}))
        audit.append("upgrade_draft_failed", item="x", why="the API answered HTTP 400: This API key is not scoped to a workspace")
        row = {r["id"]: r for r in shards.rows()}["upgrades"]
        self.assertEqual(row["state"], "blocked")
        self.assertIn("workspace", row["detail"])                                              # the actual reason, not a generic hint
        audit.append("upgrade_called", item="x", model="m", in_chars=1, out_chars=1)
        self.assertEqual({r["id"]: r for r in shards.rows()}["upgrades"]["state"], "ok")

    def test_a_missing_probe_is_unknown_not_healthy(self):
        by = {r["id"]: r for r in shards.rows()}
        self.assertEqual(by["paper_trading"]["state"], "idle")
        self.assertEqual(by["backup"]["state"], "idle")

    def test_old_reports_and_backups_are_flagged_stale(self):
        rt = Path(os.environ["AURIX_BRAIN"]) / "runtime"
        rt.mkdir(parents=True)
        (rt / "trade_report.json").write_text(json.dumps({"updated": "2020-01-01T00:00:00", "lifeforce_pct": 100.0}))
        (rt / "strategy_lab.json").write_text(json.dumps({"generated": "2020-01-01", "strategies": [], "strategies_tried": 0, "span": ["a", "b"], "inception": "x", "live_days": 0}))
        (Path(os.environ["AURIX_PROJECT_ROOT"]) / "data").mkdir(exist_ok=True)
        (Path(os.environ["AURIX_PROJECT_ROOT"]) / "data" / "backup_status.json").write_text(json.dumps({"ok": True, "message": "ok", "finished": time.time() - 40 * 3600}))
        stale = {r["id"] for r in shards.stale()}
        self.assertTrue({"paper_trading", "strategy_lab", "backup"} <= stale)
        (Path(os.environ["AURIX_PROJECT_ROOT"]) / "data" / "backup_status.json").write_text(json.dumps({"ok": True, "message": "ok", "finished": time.time() - 3600}))
        self.assertNotIn("backup", {r["id"] for r in shards.stale()})

    def test_the_digest_says_what_is_stale_or_paused(self):
        (Path(os.environ["AURIX_PROJECT_ROOT"]) / "data").mkdir(exist_ok=True)
        (Path(os.environ["AURIX_PROJECT_ROOT"]) / "data" / "backup_status.json").write_text(json.dumps({"ok": True, "message": "ok", "finished": time.time() - 50 * 3600}))
        shards.set_paused("absorb", True)
        text = heartbeat.waiting_for_you()
        self.assertIn("Nightly backup", text)
        self.assertIn("Paused by you: <code>absorb</code>", text)

    def test_text_view(self):
        t = shards.text()
        self.assertIn("Everything that works on its own", t)
        self.assertIn("spends:", t)
        self.assertIn("pause all", t)


class WiringTests(_Base):
    def test_commands(self):
        self.assertEqual(commands.parse("shards"), ("shards", ""))
        self.assertEqual(commands.parse("what's running?"), ("shards", ""))
        self.assertEqual(commands.parse("pause upgrades"), ("shard_pause", "upgrades"))
        self.assertEqual(commands.parse("pause all"), ("shard_pause", "all"))
        self.assertEqual(commands.parse("resume paper_trading"), ("shard_resume", "paper_trading"))
        self.assertEqual(commands.parse("resume"), ("resume", ""))                       # the mission `resume` is untouched
        self.assertEqual(commands.parse("pause standing sm-abc123"), ("pause_standing", "sm-abc123"))

    def test_buttons_can_look_but_pausing_is_typed_or_confirmed_on_the_page(self):
        self.assertIn("shards", buttons.ALLOWED_KINDS)
        for k in ("shard_pause", "shard_resume"):
            self.assertNotIn(k, buttons.ALLOWED_KINDS)

    def test_dashboard_actions_are_validated(self):
        self.assertIn("Paused", actions.run_action("shard_pause", "upgrades")["message"])
        self.assertIn("Resumed", actions.run_action("shard_resume", "upgrades")["message"])
        self.assertIn("Paused every", actions.run_action("shard_pause", "all")["message"])
        self.assertFalse(actions.run_action("shard_pause", "../etc")["ok"])
        self.assertFalse(actions.run_action("shard_pause", "nothing")["ok"])
        for n in ("shard_pause", "shard_resume"):
            self.assertIn(n, actions.ACTION_NAMES)

    def test_protected_and_in_the_snapshot(self):
        self.assertEqual(identity.protected_component_for("src/foundation/shards.py"), "approval_gate")
        from src.foundation import command_center
        self.assertEqual([r["id"] for r in command_center.snapshot()["shards"]], shards.IDS)


if __name__ == "__main__":
    unittest.main()
