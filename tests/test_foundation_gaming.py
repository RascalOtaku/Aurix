"""Tests for src/foundation/gaming.py (the game doctor, AURIX side): detection, proposals, signed approvals, results, and the commands."""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import approval_gate as ag  # noqa: E402
from src.foundation import audit, commands, gaming  # noqa: E402

KEY_HEX = "cd" * 32
NOW = time.time()


def recent(days=1):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def fo4(**over):
    pl = {"enabled": ["Good.esp", "Bad.esp", "Child.esp", "Late.esp"], "disabled": ["Off.esm"], "present": ["Good.esp", "Bad.esp", "Child.esp", "Late.esp", "Off.esm"],
          "masters": {"Good.esp": ["Fallout4.esm"], "Bad.esp": ["Missing.esm"], "Child.esp": ["Bad.esp"], "Late.esp": ["Off.esm"]},
          "light": [], "implicit": ["fallout4.esm", "dlcrobot.esm"]}
    pl.update(over)
    return {"appid": "377160", "name": "Fallout 4", "known": True, "size_gb": 100.9, "build": "1", "updated": 1789000000, "plugins": pl,
            "crash_events": [], "dumps": {"count": 0, "bytes": 0, "oldest_days": 0}}


def report(*games, libs=None):
    return {"schema": 1, "generated_ts": NOW, "games": list(games) or [fo4()], "libraries": libs or [{"path": "C:\\Steam", "free_gb": 500, "total_gb": 2000}]}


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": str(t), "AURIX_GAMING_DIR": str(t / "gaming"), "AURIX_GAMING_HMAC_KEY": KEY_HEX})
        self.env.start()
        ag.reset_state()
        audit._heads.clear()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
        ag.reset_state()
        audit._heads.clear()

    def put_report(self, rep):
        d = Path(os.environ["AURIX_GAMING_DIR"])
        d.mkdir(parents=True, exist_ok=True)
        (d / "report.json").write_text(json.dumps(rep), encoding="utf-8")

    def put_result(self, pid, status, detail="ok", kind="apply"):
        d = Path(os.environ["AURIX_GAMING_DIR"]) / "results"
        d.mkdir(parents=True, exist_ok=True)
        (d / (("undo-" if kind == "undo" else "") + pid + ".json")).write_text(json.dumps({"id": pid, "kind": kind, "status": status, "detail": detail}))


class AnalysisTests(unittest.TestCase):
    def test_broken_plugins_chain_and_reasons(self):
        bad = gaming.broken_plugins(fo4()["plugins"])
        self.assertEqual(set(bad), {"Bad.esp", "Child.esp", "Late.esp"})
        self.assertIn("not installed", bad["Bad.esp"])
        self.assertIn("Bad.esp", bad["Child.esp"])                              # depends on something that has to go
        self.assertIn("switched off", bad["Late.esp"])                           # Off.esm is present but not enabled

    def test_healthy_load_order_and_implicit_masters(self):
        pl = fo4(enabled=["Good.esp"], masters={"Good.esp": ["Fallout4.esm", "DLCRobot.esm"]})["plugins"]
        self.assertEqual(gaming.broken_plugins(pl), {})

    def test_regression_the_owners_real_skyrim_pattern_is_not_flagged(self):
        """2026-09-20: Vortex lists .esl files WITHOUT a star and leaves _ResourcePack.esl unlisted; the game loads them fine. The first
        version of the check called 136 plugins broken because of that: a yes would have switched off most of a working load order."""
        pl = {"enabled": ["Unofficial Skyrim Special Edition Patch.esp", "Penitus_Oculatus.esp", "LegacyoftheDragonborn.esm"],
              "disabled": ["unofficial arcane accessories patch.esl", "unofficial farming patch.esl"],
              "present": ["Unofficial Skyrim Special Edition Patch.esp", "Penitus_Oculatus.esp", "LegacyoftheDragonborn.esm", "_ResourcePack.esl",
                          "unofficial arcane accessories patch.esl", "unofficial farming patch.esl"],
              "masters": {"Unofficial Skyrim Special Edition Patch.esp": ["Skyrim.esm", "_ResourcePack.esl"],
                          "LegacyoftheDragonborn.esm": ["_ResourcePack.esl"], "Penitus_Oculatus.esp": ["Unofficial Skyrim Special Edition Patch.esp"]},
              "light": [], "implicit": ["skyrim.esm"]}
        self.assertEqual(gaming.broken_plugins(pl), {})
        pl["masters"]["Penitus_Oculatus.esp"].append("unofficial farming patch.esl")            # an explicitly unstarred .esl: still ambiguous
        self.assertEqual(gaming.broken_plugins(pl), {})

    def test_only_certain_cases_are_flagged(self):
        base = dict(enabled=["A.esp"], present=["A.esp", "Lib.esm", "Light.esl", "Off.esp", "Off.esm"], implicit=[], light=[])
        cases = [({"A.esp": ["Gone.esm"]}, [], True),                    # master file not installed at all: certain
                 ({"A.esp": ["Lib.esm"]}, [], False),                    # installed, unlisted .esm: ambiguous, left alone
                 ({"A.esp": ["Light.esl"]}, [], False),                  # installed .esl: left alone
                 ({"A.esp": ["Off.esp"]}, [], True),                     # an .esp that is not enabled: certain
                 ({"A.esp": ["Off.esm"]}, ["Off.esm"], True)]            # an .esm explicitly listed as off: certain
        for masters, disabled, expect in cases:
            got = gaming.broken_plugins({**base, "masters": masters, "disabled": disabled})
            self.assertEqual(bool(got), expect, (masters, disabled))

    def test_case_insensitive(self):
        pl = fo4(enabled=["Good.esp"], masters={"Good.esp": ["FALLOUT4.ESM"]})["plugins"]
        self.assertEqual(gaming.broken_plugins(pl), {})

    def test_missing_master_becomes_a_fixable_issue(self):
        [iss] = [i for i in gaming.analyze(report()) if i["kind"] == "missing_master"]
        self.assertEqual(iss["severity"], "high")
        self.assertEqual(iss["fix"]["actions"][0]["type"], "plugins_disable")
        self.assertEqual(iss["fix"]["actions"][0]["plugins"], ["Bad.esp", "Child.esp", "Late.esp"])
        self.assertIn("3 Fallout 4 plugins", iss["title"])

    def test_the_issue_key_changes_when_the_set_changes(self):
        a = gaming.analyze(report())[0]["key"]
        b = gaming.analyze(report(fo4(enabled=["Good.esp", "Bad.esp"])))[0]["key"]
        self.assertNotEqual(a, b)

    def test_crashes_are_information_with_no_fix(self):
        g = fo4(enabled=["Good.esp"], masters={"Good.esp": ["Fallout4.esm"]})
        g["crash_events"] = [{"exe": "Fallout4.exe", "module": "valhalla.dll", "code": "c0000005", "when": recent(1)},
                             {"exe": "Fallout4.exe", "module": "valhalla.dll", "code": "c0000005", "when": recent(2)},
                             {"exe": "Fallout4.exe", "module": "old.dll", "code": "x", "when": recent(40)}]
        [iss] = gaming.analyze(report(g))
        self.assertEqual(iss["kind"], "recent_crashes")
        self.assertIsNone(iss["fix"])
        self.assertIn("valhalla.dll (2x)", iss["evidence"][0])
        self.assertTrue(iss["hands_on"])
        self.assertNotIn("Claude", iss["hands_on"])

    def test_a_crash_naming_one_mod_dll_becomes_a_yes_no_fix(self):
        g = fo4(enabled=["Good.esp"], masters={"Good.esp": ["Fallout4.esm"]})
        g["mod_dlls"] = ["Crashy.dll", "Fine.dll"]
        g["crash_events"] = [{"exe": "Fallout4.exe", "module": "crashy.dll", "code": "c0000005", "when": recent(1)},
                             {"exe": "Fallout4.exe", "module": "Crashy.dll", "code": "c0000005", "when": recent(2)},
                             {"exe": "Fallout4.exe", "module": "ntdll.dll", "code": "c0000374", "when": recent(3)}]
        [iss] = gaming.analyze(report(g))
        self.assertEqual(iss["kind"], "crashing_mod")
        self.assertEqual(iss["fix"]["actions"], [{"type": "quarantine_dll", "dll": "Crashy.dll"}])
        text = " ".join([iss["title"]] + iss["evidence"] + [iss["fix"]["summary"], iss["fix"]["risk"]])
        self.assertIn("2 of 3 crashes", text)
        self.assertIn("ntdll.dll (1x)", text)
        self.assertNotIn("Claude", text)

    def test_one_crash_or_a_system_module_is_never_pinned_on_a_mod(self):
        g = fo4(enabled=["Good.esp"], masters={"Good.esp": ["Fallout4.esm"]})
        g["mod_dlls"] = ["Crashy.dll", "ntdll.dll"]
        g["crash_events"] = [{"exe": "x", "module": "Crashy.dll", "code": "c", "when": recent(1)}]
        self.assertEqual(gaming.analyze(report(g))[0]["kind"], "recent_crashes")
        g["crash_events"] = [{"exe": "x", "module": "ntdll.dll", "code": "c", "when": recent(1)}, {"exe": "x", "module": "ntdll.dll", "code": "c", "when": recent(2)}]
        [iss] = gaming.analyze(report(g))
        self.assertEqual(iss["kind"], "recent_crashes")
        self.assertIn("game or Windows itself", iss["hands_on"])
        self.assertNotIn("Claude", iss["hands_on"])

    def test_crashes_from_a_mod_you_already_had_moved_out_are_history(self):
        g = fo4(enabled=["Good.esp"], masters={"Good.esp": ["Fallout4.esm"]})
        g["crash_events"] = [{"exe": "Fallout4.exe", "module": "Crashy.dll", "code": "c", "when": recent(1)}, {"exe": "Fallout4.exe", "module": "Crashy.dll", "code": "c", "when": recent(2)}]
        self.assertEqual(gaming.analyze(report(g))[0]["kind"], "recent_crashes")
        self.assertEqual(gaming.analyze(report(g), {("377160", "crashy.dll")}), [])
        g["crash_events"].append({"exe": "Fallout4.exe", "module": "Other.dll", "code": "c", "when": recent(1)})
        [iss] = gaming.analyze(report(g), {("377160", "crashy.dll")})
        self.assertIn("Other.dll", iss["evidence"][0])                                     # a different crash still counts

    def test_dumps_and_low_disk_and_plugin_limit(self):
        g = fo4(enabled=[f"p{i}.esp" for i in range(260)], masters={}, present=[], light=[])
        g["dumps"] = {"count": 5, "bytes": 900e6, "oldest_days": 30}
        kinds = {i["kind"] for i in gaming.analyze(report(g, libs=[{"path": "D:\\S", "free_gb": 5, "total_gb": 100}]))}
        self.assertEqual(kinds, {"plugin_limit", "crash_dumps", "low_disk"})
        dump = [i for i in gaming.analyze(report(g)) if i["kind"] == "crash_dumps"][0]
        self.assertEqual(dump["fix"]["actions"][0], {"type": "delete_dumps", "older_than_days": 7})

    def test_a_clean_report_has_no_issues(self):
        self.assertEqual(gaming.analyze(report(fo4(enabled=["Good.esp"], masters={"Good.esp": ["Fallout4.esm"]}))), [])


class ProposalTests(_Base):
    def test_sync_creates_pending_and_info_and_does_not_repeat(self):
        g = fo4()
        g["crash_events"] = [{"exe": "Fallout4.exe", "module": "m.dll", "code": "c", "when": recent(1)}]
        fresh = gaming.sync_proposals(gaming.analyze(report(g)), NOW)
        self.assertEqual(sorted(p["status"] for p in fresh), ["info", "pending"])
        self.assertEqual(gaming.sync_proposals(gaming.analyze(report(g)), NOW + 60), [])           # nothing new
        self.assertEqual(len(gaming.pending()), 1)

    def test_declined_stays_quiet_then_returns(self):
        [p] = gaming.sync_proposals(gaming.analyze(report()), NOW)
        gaming.decline(p["id"], NOW)
        self.assertEqual(gaming.sync_proposals(gaming.analyze(report()), NOW + 5 * 86400), [])
        self.assertEqual(len(gaming.sync_proposals(gaming.analyze(report()), NOW + 40 * 86400)), 1)

    def test_render_is_a_clear_cut_report(self):
        [p] = gaming.sync_proposals(gaming.analyze(report()), NOW)
        text = gaming.render_proposal(p)
        for needle in ("would crash the game at startup", "What I found", "What I will change", "Risk:", f"yes {p['id']}", f"no {p['id']}", f"undo {p['id']}",
                       "Bad.esp needs Missing.esm"):
            self.assertIn(needle, text)

    def test_old_notes_that_pointed_elsewhere_are_rewritten(self):
        gaming.save_proposal({"id": "g-abc123", "key": "k", "game": "G", "appid": "1", "kind": "recent_crashes", "severity": "warn", "title": "t", "evidence": ["e"], "fix": None,
                              "hands_on": "Needs a hands-on session. Ask a Claude Code session on the PC.", "status": "info", "created": NOW})
        gaming.sync_proposals([], NOW)
        self.assertNotIn("Claude", gaming.load_proposal("g-abc123")["hands_on"])

    def test_a_stale_note_is_replaced_by_the_fix_that_names_the_mod(self):
        base = {"appid": "489830", "game": "Skyrim", "severity": "warn", "fix": None, "created": NOW}
        gaming.save_proposal({**base, "id": "g-aaaaaa", "key": "k1", "kind": "recent_crashes", "title": "t", "evidence": ["crashing module: valhallaCombat.dll (2x)"],
                              "hands_on": "The crash points at the game or Windows itself (the module named above), not at one mod", "status": "info"})
        gaming.save_proposal({**base, "id": "g-bbbbbb", "key": "k2", "kind": "crashing_mod", "title": "t2", "evidence": ["e"], "status": "pending",
                              "fix": {"actions": [{"type": "quarantine_dll", "dll": "valhallaCombat.dll"}], "summary": "s", "risk": "r"}})
        gaming.sync_proposals([], NOW)
        old = gaming.load_proposal("g-aaaaaa")
        self.assertEqual(old["status"], "superseded")
        self.assertIn("g-bbbbbb", old["hands_on"])
        self.assertEqual(gaming.load_proposal("g-bbbbbb")["status"], "pending")

    def test_info_reports_have_no_yes_no(self):
        g = fo4(enabled=["Good.esp"], masters={"Good.esp": ["Fallout4.esm"]})
        g["crash_events"] = [{"exe": "x", "module": "m.dll", "code": "c", "when": recent(1)}]
        [p] = gaming.sync_proposals(gaming.analyze(report(g)), NOW)
        text = gaming.render_proposal(p)
        self.assertNotIn("yes g-", text)
        self.assertIn("What now", text)
        self.assertNotIn("Claude", text)

    def test_text_from_the_report_is_escaped(self):
        g = fo4(enabled=["<b>x</b>.esp"], masters={"<b>x</b>.esp": ["<i>m</i>.esm"]}, present=[])
        [p] = gaming.sync_proposals(gaming.analyze(report(g)), NOW)
        self.assertNotIn("<i>m</i>", gaming.render_proposal(p))


class ApprovalTests(_Base):
    def _pending(self):
        [p] = gaming.sync_proposals(gaming.analyze(report()), NOW)
        return p

    def test_approve_writes_a_signed_expiring_file_and_audits(self):
        p = self._pending()
        self.assertIn("Approved", gaming.approve(p["id"], NOW))
        f = Path(os.environ["AURIX_GAMING_DIR"]) / "approved" / f"{p['id']}.json"
        item = json.loads(f.read_text())
        self.assertEqual(item["sig"], gaming.sign(bytes.fromhex(KEY_HEX), item))
        self.assertEqual(item["actions"][0]["plugins"], ["Bad.esp", "Child.esp", "Late.esp"])
        self.assertGreater(datetime.fromisoformat(item["expires"]).timestamp(), NOW + 23 * 3600)
        self.assertEqual(gaming.load_proposal(p["id"])["status"], "approved")
        self.assertIn("gaming_fix_approved", json.dumps(audit.recent(5)))
        self.assertIn("nothing to approve", gaming.approve(p["id"], NOW))                             # not twice

    def test_without_the_key_nothing_is_written(self):
        p = self._pending()
        with mock.patch.dict(os.environ, {"AURIX_GAMING_HMAC_KEY": ""}):
            self.assertIn("cannot sign", gaming.approve(p["id"], NOW))
        self.assertFalse((Path(os.environ["AURIX_GAMING_DIR"]) / "approved").exists())
        self.assertEqual(gaming.load_proposal(p["id"])["status"], "pending")

    def test_ids_are_validated_and_unknown_ones_refused(self):
        self.assertIsNone(gaming.load_proposal("../../x"))
        self.assertIn("No game fix", gaming.approve("g-ffffff"))
        self.assertIn("No game fix", gaming.decline("g-ffffff"))

    def test_info_cannot_be_approved(self):
        g = fo4(enabled=["Good.esp"], masters={"Good.esp": ["Fallout4.esm"]})
        g["crash_events"] = [{"exe": "x", "module": "m.dll", "code": "c", "when": recent(1)}]
        [p] = gaming.sync_proposals(gaming.analyze(report(g)), NOW)
        self.assertIn("nothing to approve", gaming.approve(p["id"]))

    def test_results_update_status_and_message_once(self):
        p = self._pending()
        gaming.approve(p["id"], NOW)
        self.put_result(p["id"], "applied", [{"type": "plugins_disable", "disabled": ["Bad.esp"]}])
        msgs = gaming.ingest_results()
        self.assertEqual(len(msgs), 1)
        self.assertIn("Fixed", msgs[0])
        self.assertIn(f"undo {p['id']}", msgs[0])
        self.assertEqual(gaming.load_proposal(p["id"])["status"], "applied")
        self.assertEqual(gaming.ingest_results(), [])                                                # once only

    def test_failed_and_refused_results_are_reported_plainly(self):
        p = self._pending()
        gaming.approve(p["id"], NOW)
        self.put_result(p["id"], "refused", "bad signature: not applied")
        [m] = gaming.ingest_results()
        self.assertIn("Not applied", m)
        self.assertIn("bad signature", m)
        self.assertEqual(gaming.load_proposal(p["id"])["status"], "failed")

    def test_undo_flow(self):
        p = self._pending()
        self.assertIn("nothing to undo", gaming.undo(p["id"]))                                       # not applied yet
        gaming.approve(p["id"], NOW)
        self.put_result(p["id"], "applied")
        gaming.ingest_results()
        self.assertIn("Undo requested", gaming.undo(p["id"], NOW))
        item = json.loads((Path(os.environ["AURIX_GAMING_DIR"]) / "approved" / f"undo-{p['id']}.json").read_text())
        self.assertEqual((item["kind"], item["target"]), ("undo", p["id"]))
        self.assertEqual(item["sig"], gaming.sign(bytes.fromhex(KEY_HEX), item))
        self.put_result(p["id"], "undone", "restored", kind="undo")
        [m] = gaming.ingest_results()
        self.assertIn("Undone", m)
        self.assertEqual(gaming.load_proposal(p["id"])["status"], "undone")


class ReadBackTests(_Base):
    def test_games_and_game_text(self):
        self.assertIn("no report", gaming.games_text())
        self.put_report(report(fo4(), {"appid": "1", "name": "Small Game", "known": False, "size_gb": 1.0, "build": "5", "updated": 0}))
        t = gaming.games_text()
        self.assertIn("Fallout 4", t)
        self.assertIn("Small Game", t)
        self.assertLess(t.index("Fallout 4"), t.index("Small Game"))                                 # biggest first
        gaming.sync_proposals(gaming.analyze(report()), NOW)
        self.assertIn("1 fix waiting", gaming.games_text())
        g = gaming.game_text("fallout")
        self.assertIn("3 would break loading", g)
        self.assertIn("4 enabled of 5 installed", g)
        self.assertIn("do not see a game", gaming.game_text("zelda"))

    def test_stale_report_is_flagged(self):
        rep = report()
        rep["generated_ts"] = NOW - 10 * 3600
        self.put_report(rep)
        self.assertIn("stale", gaming.games_text(NOW))

    async def test_ask_answers_from_the_report_only(self):
        self.put_report(report())
        seen = {}

        async def llm(system, prompt):
            seen["system"], seen["prompt"] = system, prompt
            return "Fallout 4 has 3 broken plugins <script>"
        out = await gaming.ask("what is wrong with fallout?", llm)
        self.assertIn("ONLY the JSON report", seen["system"])
        self.assertIn("Bad.esp", seen["system"])
        self.assertNotIn("<script>", out)                                                            # escaped
        self.assertEqual(seen["prompt"], "what is wrong with fallout?")

    async def test_ask_without_a_model_or_report_falls_back(self):
        self.assertIn("no report", await gaming.ask("q", None))
        self.put_report(report())
        self.assertIn("Your Steam games", await gaming.ask("q", None))

        async def boom(s, p):
            raise RuntimeError("down")
        self.assertIn("Your Steam games", await gaming.ask("q", boom))

    def test_request_refresh(self):
        self.assertIn("Asked your PC", gaming.request_refresh())
        self.assertTrue((Path(os.environ["AURIX_GAMING_DIR"]) / "requests" / "collect-now").exists())


class TickTests(_Base):
    def test_a_new_report_produces_proposals_once_and_results_are_relayed(self):
        self.put_report(report())
        msgs = gaming.tick(NOW)
        self.assertEqual(len(msgs), 1)
        self.assertIn("would crash the game", msgs[0])
        self.assertEqual(gaming.tick(NOW + 60), [])                                                  # same report: silent
        p = gaming.pending()[0]
        gaming.approve(p["id"], NOW)
        self.put_result(p["id"], "applied")
        self.assertIn("Fixed", gaming.tick(NOW + 120)[0])

    def test_burst_of_new_issues_is_capped(self):
        games = [dict(fo4(), appid=str(i), name=f"Game {i}") for i in range(6)]
        self.put_report(report(*games))
        msgs = gaming.tick(NOW)
        self.assertEqual(len(msgs), gaming.MAX_NEW_PER_TICK + 1)
        self.assertIn("more game notes", msgs[-1])

    def test_no_report_is_quiet(self):
        self.assertEqual(gaming.tick(NOW), [])


class CommandTests(_Base):
    def setUp(self):
        super().setUp()
        self.notified = []

        async def notify(t):
            self.notified.append(t)

        async def agent(p):
            return "ok"

        async def llm(system, prompt):
            return "answer from the report"
        self.f = commands.Foundation(agent, notify, llm=llm, session_id="tg", env={}, which=lambda b: None, find_spec=lambda m: None,
                                     probe=lambda h, p: False, authorizations={})

    def test_parse_variants(self):
        p = commands.parse
        self.assertEqual(p("games"), ("games", ""))
        self.assertEqual(p("Steam refresh"), ("games_refresh", ""))
        self.assertEqual(p("game skyrim"), ("game", "skyrim"))
        self.assertEqual(p("yes g-abc123"), ("fix_yes", "g-abc123"))
        self.assertEqual(p("NO G-ABC123."), ("fix_no", "g-abc123"))
        self.assertEqual(p("undo g-abc123"), ("fix_undo", "g-abc123"))
        self.assertEqual(p("what mods do I have in skyrim?")[0], "games_ask")
        self.assertIsNone(p("yes"))                                                                  # nothing waiting: never swallowed
        self.assertEqual(p("approve mission m-abc123")[0], "approve_mission")                        # existing commands keep priority
        self.assertIsNone(p("thanks, see you tomorrow"))

    def test_a_bare_yes_or_no_answers_the_single_waiting_fix_only(self):
        [pr] = gaming.sync_proposals(gaming.analyze(report()), NOW)
        self.assertEqual(commands.parse("yes"), ("fix_yes", pr["id"]))
        self.assertEqual(commands.parse("No!"), ("fix_no", pr["id"]))
        gaming.sync_proposals(gaming.analyze(report(dict(fo4(), appid="9", name="Other"))), NOW)     # a second one waiting
        self.assertIsNone(commands.parse("yes"))                                                     # ambiguous: refuse to guess

    async def test_end_to_end_yes_flow(self):
        self.put_report(report())
        await self.f.tick_standing(now=NOW)
        self.assertTrue(any("would crash the game" in m for m in self.notified))
        pid = gaming.pending()[0]["id"]
        self.assertIn("Approved", await self.f.handle("fix_yes", pid))
        self.assertIn("No game fixes are waiting", await self.f.handle("fixes", ""))
        self.assertIn("Fallout 4", await self.f.handle("games", ""))
        self.assertIn("answer from the report", await self.f.handle("games_ask", "what is wrong?"))
        self.assertIn("Asked your PC", await self.f.handle("games_refresh", ""))

    async def test_no_declines(self):
        self.put_report(report())
        gaming.tick(NOW)
        pid = gaming.pending()[0]["id"]
        self.assertIn("Declined", await self.f.handle("fix_no", pid))
        self.assertEqual(gaming.pending(), [])

    def test_digest_line(self):
        self.assertEqual(gaming.digest_line(), "")
        gaming.sync_proposals(gaming.analyze(report()), NOW)
        self.assertIn("1 fix waiting", gaming.digest_line())


if __name__ == "__main__":
    unittest.main()
