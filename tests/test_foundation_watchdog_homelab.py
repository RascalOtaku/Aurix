"""Watchdog additions: memory pressure, an early disk heads-up, homelab services that stop answering, and the reboot report."""
import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.foundation import audit, commands, qol, sysview, watchdog  # noqa: E402


def snap(disk=50.0, mem=40.0, swap=10.0, uptime_h=100.0, homelab=None, extra_organs=None):
    organs = {"circulatory": {"title": "Circulation", "status": "ok",
                              "metrics": {"disk %": disk, "memory %": mem, "swap %": swap, "uptime h": uptime_h}}}
    organs.update(extra_organs or {})
    s = {"organs": organs, "audit": {"ok": True}, "missions": [], "standing": []}
    if homelab is not None:
        s["homelab"] = homelab
    return s


def row(name, ok, url="http://h:1"):
    return {"name": name, "url": url, "ok": ok, "detail": "answering" if ok else "not answering"}


def run(snapshot, state=None, now=1_000_000.0):
    return watchdog.evaluate(snapshot, state or {}, now)


class MemoryAndDiskTests(unittest.TestCase):
    def test_memory_tight_by_ram_or_by_swap(self):
        self.assertEqual(run(snap(mem=60, swap=65))[0], [])                                     # today's real numbers: stale swap alone is not an alarm
        for kwargs, expect in (({"mem": 93}, "RAM 93%"), ({"swap": 90}, "swap 90%"), ({"mem": 95, "swap": 88}, "RAM 95%, swap 88%")):
            msgs, state = run(snap(**kwargs))
            self.assertEqual(len(msgs), 1, kwargs)
            self.assertIn("Memory is tight", msgs[0])
            self.assertIn(expect, msgs[0])
        self.assertEqual(run(snap(mem=91.9, swap=84.9))[0], [])                                 # just under both limits

    def test_missing_memory_metrics_do_not_crash(self):
        organs = {"circulatory": {"title": "c", "status": "ok", "metrics": {}}}
        self.assertEqual(run({"organs": organs, "audit": {"ok": True}})[0], [])

    def test_disk_gets_an_early_warning_then_the_real_alarm_never_both(self):
        self.assertEqual(run(snap(disk=84.9))[0], [])
        early = run(snap(disk=87))[0]
        self.assertEqual(len(early), 1)
        self.assertIn("87% full", early[0])
        self.assertIn("before it hits 92%", early[0])
        late = run(snap(disk=96))[0]
        self.assertEqual(len(late), 1)
        self.assertIn("96% full", late[0])
        self.assertNotIn("before it hits", late[0])

    def test_problems_alert_once_then_clear(self):
        msgs, st = run(snap(swap=90), now=1000.0)
        self.assertEqual(len(msgs), 1)
        again, st = run(snap(swap=90), st, now=1000.0 + 300)
        self.assertEqual(again, [])                                                             # not repeated within 6 h
        cleared, st = run(snap(swap=20), st, now=1000.0 + 600)
        self.assertEqual(len(cleared), 1)
        self.assertIn("Cleared", cleared[0])


class HomelabTests(unittest.TestCase):
    def test_a_service_that_never_answered_is_never_reported(self):
        # the 3431 is simply switched off: alerting about it forever would be noise
        st = {}
        for i in range(6):
            msgs, st = run(snap(homelab=[row("3431 dashboard", False)]), st, now=1000.0 + i * 300)
            self.assertEqual(msgs, [], i)
        self.assertEqual(st["homelab_up"], {})

    def test_a_service_that_was_up_and_stops_is_reported_after_two_checks(self):
        msgs, st = run(snap(homelab=[row("Jellyfin", True)]))
        self.assertEqual((msgs, st["homelab_up"]), ([], {"Jellyfin": True}))
        first_down, st = run(snap(homelab=[row("Jellyfin", False)]), st, now=1_000_300.0)
        self.assertEqual(first_down, [])                                                        # one failed probe is noise
        second_down, st = run(snap(homelab=[row("Jellyfin", False)]), st, now=1_000_600.0)
        self.assertEqual(len(second_down), 1)
        self.assertIn("Jellyfin", second_down[0])
        self.assertIn("stopped answering", second_down[0])
        repeat, st = run(snap(homelab=[row("Jellyfin", False)]), st, now=1_000_900.0)
        self.assertEqual(repeat, [])
        back, st = run(snap(homelab=[row("Jellyfin", True)]), st, now=1_001_200.0)
        self.assertEqual(len(back), 1)
        self.assertIn("Cleared", back[0])

    def test_a_quiet_link_that_goes_down_is_never_reported_but_a_normal_one_still_is(self):
        rows_up = [dict(row("Gaming PC", True), quiet=True), row("Jellyfin", True)]
        rows_down = [dict(row("Gaming PC", False), quiet=True), row("Jellyfin", False)]
        _, st = run(snap(homelab=rows_up))
        self.assertEqual(st["homelab_up"], {"Gaming PC": True, "Jellyfin": True})            # it IS tracked as having been up...
        joined = []
        for i in (1, 2, 3):
            msgs, st = run(snap(homelab=rows_down), st, now=1_000_000.0 + i * 300)
            joined += msgs
        self.assertEqual(len([m for m in joined if "stopped answering" in m]), 1)
        self.assertIn("Jellyfin", "".join(joined))
        self.assertNotIn("Gaming PC", "".join(joined))                                        # ...but a sleeping PC pages nobody

    def test_a_one_probe_blip_never_alerts(self):
        _, st = run(snap(homelab=[row("Pi-hole", True)]))
        blip, st = run(snap(homelab=[row("Pi-hole", False)]), st, now=1_000_300.0)
        recovered, st = run(snap(homelab=[row("Pi-hole", True)]), st, now=1_000_600.0)
        again, st = run(snap(homelab=[row("Pi-hole", False)]), st, now=1_000_900.0)
        self.assertEqual((blip, recovered, again), ([], [], []))                                # strikes reset on recovery

    def test_services_are_tracked_independently_and_names_are_escaped(self):
        _, st = run(snap(homelab=[row("A", True), row("B <b>x</b>", True), row("Off", False)]))
        for i in (1, 2):
            msgs, st = run(snap(homelab=[row("A", True), row("B <b>x</b>", False), row("Off", False)]), st, now=1_000_000.0 + i * 300)
        self.assertEqual(len(msgs), 1)
        self.assertIn("B &lt;b&gt;x&lt;/b&gt;", msgs[0])
        self.assertNotIn("<b>x</b>", msgs[0])

    def test_no_homelab_key_is_fine(self):
        self.assertEqual(run(snap())[0], [])


class BootTests(unittest.TestCase):
    NOW = 2_000_000.0

    def test_first_sight_is_remembered_silently(self):
        text, boot = watchdog.boot_check(snap(uptime_h=50), {}, self.NOW)
        self.assertIsNone(text)
        self.assertAlmostEqual(boot, self.NOW - 50 * 3600)

    def test_the_same_boot_seen_again_says_nothing_even_with_rounding_wobble(self):
        _, boot = watchdog.boot_check(snap(uptime_h=50.0), {}, self.NOW)
        for later, up in ((300, 50.1), (900, 50.3), (3600, 51.0)):
            text, remembered = watchdog.boot_check(snap(uptime_h=up), {"last_boot": boot}, self.NOW + later)
            self.assertIsNone(text, later)
            self.assertEqual(remembered, boot)

    def test_a_reboot_is_reported_once_with_the_state_it_came_back_in(self):
        old_boot = self.NOW - 600 * 3600
        s = snap(uptime_h=0.2, disk=52, mem=41, swap=3, extra_organs={"nervous": {"title": "Nervous system", "status": "warn", "metrics": {}}})
        text, boot = watchdog.boot_check(s, {"last_boot": old_boot}, self.NOW)
        self.assertIn("server rebooted", text)
        self.assertIn("disk 52%", text)
        self.assertIn("RAM 41%", text)
        self.assertIn("swap 3%", text)
        self.assertIn("Needs a look: Nervous system (warn)", text)
        self.assertAlmostEqual(boot, self.NOW - 0.2 * 3600)
        again, boot2 = watchdog.boot_check(snap(uptime_h=0.3), {"last_boot": boot}, self.NOW + 360)
        self.assertIsNone(again)                                                               # once per reboot

    def test_all_normal_wording_and_waiting_for_the_containers_to_settle(self):
        old = self.NOW - 100 * 3600
        # 1 minute after boot: hold the report (containers still starting) and keep the OLD value so the next check reports it
        text, kept = watchdog.boot_check(snap(uptime_h=1 / 60), {"last_boot": old}, self.NOW)
        self.assertEqual((text, kept), (None, old))
        text, _ = watchdog.boot_check(snap(uptime_h=0.1), {"last_boot": old}, self.NOW + 300)
        self.assertIn("All systems normal", text)

    def test_missing_uptime_never_crashes_and_keeps_state(self):
        organs = {"circulatory": {"title": "c", "status": "ok", "metrics": {}}}
        self.assertEqual(watchdog.boot_check({"organs": organs}, {"last_boot": 5.0}, self.NOW), (None, 5.0))
        self.assertEqual(watchdog.boot_check({}, {}, self.NOW), (None, None))

    def test_evaluate_carries_the_boot_and_homelab_state_forward(self):
        _, st = run(snap(homelab=[row("X", True)]), {"last_boot": 123.0, "homelab_up": {"Old": True}})
        self.assertEqual(st["last_boot"], 123.0)
        self.assertEqual(st["homelab_up"], {"Old": True, "X": True})


class TickIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name, "AURIX_WATCHDOG_MINUTES": "1", "AURIX_TZ_OFFSET_MINUTES": "0"})
        self.env.start()
        audit._heads.clear()

    def tearDown(self):
        audit._heads.clear()
        self.env.stop()
        self.tmp.cleanup()

    async def test_reboot_and_homelab_alerts_flow_through_the_real_tick(self):
        sent = []

        async def notify(t):
            sent.append(t)

        async def agent(t):
            return ""
        f = commands.Foundation(agent, notify, llm=None)
        state = {"n": 0}
        seq = [snap(uptime_h=700.0, homelab=[row("Jellyfin", True)]),                    # first look: remembered silently
               snap(uptime_h=0.2, homelab=[row("Jellyfin", True)]),                      # rebooted
               snap(uptime_h=0.3, homelab=[row("Jellyfin", False)]),                     # first failed probe
               snap(uptime_h=0.4, homelab=[row("Jellyfin", False)])]                     # second: alert

        def fake_snapshot(*a, **k):
            state["n"] += 1
            return seq[state["n"] - 1]
        rows = {"i": 0}

        def fake_homelab():
            rows["i"] += 1
            return seq[rows["i"] - 1]["homelab"]
        with mock.patch.object(commands.sysview, "snapshot", side_effect=fake_snapshot), \
                mock.patch("src.foundation.command_center.probe_homelab", side_effect=fake_homelab):
            for i in range(4):
                await f.tick_watchdog(now=1_000_000.0 + i * 120)
        joined = "\n".join(sent)
        self.assertEqual(len([t for t in sent if "server rebooted" in t]), 1)
        self.assertEqual(len([t for t in sent if "stopped answering" in t]), 1)
        self.assertTrue(sent[0].startswith("🔄"))
        self.assertIn("Jellyfin", joined)
        events = [__import__("json").loads(l)["event"] for l in open(audit.audit_path(), encoding="utf-8")]
        self.assertEqual(events.count("watchdog_alert"), 2)

    async def test_a_broken_homelab_probe_never_breaks_the_watchdog(self):
        async def notify(t):
            pass

        async def agent(t):
            return ""
        f = commands.Foundation(agent, notify, llm=None)
        with mock.patch.object(commands.sysview, "snapshot", return_value=snap(disk=96)), \
                mock.patch("src.foundation.command_center.probe_homelab", side_effect=RuntimeError("dns down")):
            sent = await f.tick_watchdog(now=1_000_000.0)
        self.assertTrue(any("96% full" in m for m in sent))                              # the other checks still ran


class HomelabCommandTests(unittest.TestCase):
    def test_text_lists_each_service_and_the_down_ones_say_why(self):
        text = qol.homelab_text([row("Jellyfin", True, "http://10.0.0.75:8096"), row("3431 dashboard", False, "http://100.64.0.11:3000")])
        self.assertIn("1 of 2 not answering", text)
        self.assertIn("✅ Jellyfin", text)
        self.assertIn("❌ 3431 dashboard", text)
        self.assertIn("http://100.64.0.11:3000", text)
        self.assertIn("not answering", text.split("3431 dashboard")[1])
        self.assertIn("everything answering", qol.homelab_text([row("A", True)]))
        self.assertIn("No homelab links", qol.homelab_text([]))

    def test_parse_and_help(self):
        for text in ("homelab", "Homelab?", "/homelab", "home lab"):
            self.assertEqual(commands.parse(text), ("homelab", ""), text)
        for text in ("my homelab is slow", "homelab status please", "services"):
            self.assertIsNone(commands.parse(text), text)
        self.assertIn("<code>homelab</code>", commands.HELP)

    def test_swap_reaches_the_health_probe_and_the_organ_metrics(self):
        fake_vm = mock.Mock(percent=41.0, total=7.7e9)
        fake_sw = mock.Mock(percent=66.0, total=10.7e9)
        fake_psutil = mock.Mock(cpu_percent=lambda interval=None: 3.0, virtual_memory=lambda: fake_vm, swap_memory=lambda: fake_sw,
                                boot_time=lambda: 0.0)
        with mock.patch.dict(sys.modules, {"psutil": fake_psutil}):
            out = sysview.probe_system()
        self.assertEqual((out["swap_pct"], out["mem_pct"]), (66.0, 41.0))
        organs = sysview.snapshot({"system": lambda: out, "endpoints": lambda: [], "sandbox": lambda: {"available": True},
                                   "telegram": lambda: {}, "governor": lambda: {}})["organs"]
        self.assertEqual(organs["circulatory"]["metrics"]["swap %"], 66.0)


if __name__ == "__main__":
    unittest.main()
