"""Command Center: assembly that fails soft, who may decide approvals (never the agent), and the page's CSP safety."""
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import approval_gate as ag  # noqa: E402
from src.foundation import audit, command_center as cc, sysview, xp  # noqa: E402

SYS = {
    "system": lambda: {"cpu_pct": 5.0, "mem_pct": 40.0, "disk_pct": 50.0},
    "endpoints": lambda: [{"name": "gpu-box", "ok": True, "url": "x", "loaded": [], "models": None}],
    "sandbox": lambda: {"available": True, "tools_present": 14, "tools_total": 19, "openhands": True, "missing": []},
    "telegram": lambda: {"configured": True, "listener_alive": True, "scheduler_alive": True, "in_flight": 0},
    "governor": lambda: {"mode": "aurix"},
}
EXTRA = {"memory": lambda: {"wiki_pages": 57, "rag_chunks": 110, "graph": {"pages": 118, "links": 102, "chunks": 117, "tags": 5},
                            "gaps": [{"title": "User Vault", "refs": 4}]},
         "skills": lambda: {"total": 1, "active": 1, "items": [{"name": "csv_cleaner", "status": "active", "description": "d", "uses": 3}]}}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name, "AURIX_TZ_OFFSET_MINUTES": "0"})
        self.env.start()
        self.net = mock.patch.object(cc, "tcp_up", lambda host, port, timeout=1.0: True)     # never touch the network in tests
        self.net.start()
        cc._homelab_cache.update(key=None, at=0.0, value=[])
        sysview._verify_cache.update(key=None, at=0.0, value=None)
        xp._cache.update(key=None, records=[])
        ag.reset_state()

    def tearDown(self):
        self.net.stop()
        self.env.stop()
        self.tmp.cleanup()
        ag.reset_state()


class SnapshotTests(_Base):
    def test_shape(self):
        audit.append("mission_completed", mission="m-1")
        s = cc.snapshot(SYS, EXTRA, now=1_800_000_000, godseye_url="http://host:4173/")
        for key in ("ts", "local_time", "xp", "health", "approvals", "missions", "standing", "events", "memory", "skills", "links"):
            self.assertIn(key, s)
        self.assertEqual(s["xp"]["total"], 50)
        self.assertEqual(s["links"], {"godseye": "http://host:4173/", "systems": "/systems", "chat": "/", "kuma": ""})
        for key in ("tasks", "services", "running"):
            self.assertIn(key, s)
        self.assertEqual(len(s["health"]["organs"]), 8)
        self.assertTrue(s["health"]["audit_ok"])
        self.assertEqual(s["memory"]["graph"]["pages"], 118)
        self.assertEqual(s["skills"]["items"][0]["name"], "csv_cleaner")

    def test_health_overall_is_the_worst_organ(self):
        self.assertEqual(cc.snapshot(SYS, EXTRA)["health"]["overall"], "ok")
        bad = dict(SYS, sandbox=lambda: {"available": False})
        self.assertEqual(cc.snapshot(bad, EXTRA)["health"]["overall"], "warn")
        audit.append("first")
        audit.append("second")
        p = audit.audit_path()
        p.write_text(p.read_text(encoding="utf-8").replace("first", "FORGED"), encoding="utf-8")
        sysview._verify_cache.update(key=None, at=0.0, value=None)
        self.assertEqual(cc.snapshot(bad, EXTRA)["health"]["overall"], "bad")

    def test_every_section_fails_soft(self):
        def boom():
            raise RuntimeError("down")
        s = cc.snapshot(dict(SYS, endpoints=boom), {"memory": boom, "skills": boom})
        self.assertEqual(s["memory"], {})
        self.assertEqual(s["skills"], {"total": 0, "active": 0, "pending": 0, "items": []})
        with mock.patch.object(xp, "summary", side_effect=RuntimeError("no audit")):
            self.assertIsNone(cc.snapshot(SYS, EXTRA)["xp"])

    def test_pending_approvals_are_included(self):
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            ag._pending["abc123"] = ag.PendingApproval(id="abc123", tool="bash", preview="rm -rf /tmp/x", session_id="s", owner="o",
                                                        created=1.0, future=loop.create_future(), risk="HIGH")
            s = cc.snapshot(SYS, EXTRA, now=31.0)
        finally:
            loop.close()
        self.assertEqual(s["approvals"][0]["id"], "abc123")
        self.assertEqual(s["approvals"][0]["age_s"], 30)


class ProbeTests(_Base):
    def test_skills_probe_reads_forge_dir_and_survives_garbage(self):
        forge = Path(self.tmp.name) / "data" / "forge"
        (forge / "csv_cleaner").mkdir(parents=True)
        (forge / "csv_cleaner" / "skill.json").write_text(json.dumps({"name": "csv_cleaner", "status": "active", "description": "x" * 500, "uses": 4}))
        (forge / "broken").mkdir()
        (forge / "broken" / "skill.json").write_text("{not json")
        (forge / "draft").mkdir()
        (forge / "draft" / "skill.json").write_text(json.dumps({"status": "proposed"}))
        out = cc.probe_skills()
        self.assertEqual((out["total"], out["active"]), (2, 1))
        self.assertEqual(out["items"][0]["name"], "csv_cleaner")
        self.assertLessEqual(len(out["items"][0]["description"]), 120)
        self.assertEqual(out["items"][1]["name"], "draft")                     # falls back to the directory name

    def test_skills_probe_with_no_forge_dir(self):
        self.assertEqual(cc.probe_skills(), {"total": 0, "active": 0, "pending": 0, "items": []})

    def test_memory_probe_counts_the_mounted_brain_and_ignores_missing_stores(self):
        brain = Path(self.tmp.name) / "brain"
        (brain / "wiki" / "concepts").mkdir(parents=True)
        (brain / "wiki" / "a.md").write_text("x")
        (brain / "wiki" / "concepts" / "b.md").write_text("x")
        (brain / "wiki" / "concepts" / "c.txt").write_text("x")
        (brain / "memory" / "longterm").mkdir(parents=True)
        (brain / "memory" / "longterm" / "n.md").write_text("x")
        with mock.patch.dict(os.environ, {"AURIX_BRAIN": str(brain), "NEO4J_URI": "", "NEO4J_PASSWORD": ""}):
            out = cc.probe_memory()
        self.assertEqual((out["wiki_pages"], out["longterm_notes"]), (2, 1))
        self.assertNotIn("graph", out)                                        # Neo4j not configured: simply absent

    def test_memory_probe_with_nothing_mounted(self):
        with mock.patch.dict(os.environ, {"AURIX_BRAIN": "/definitely/not/here", "NEO4J_URI": "", "NEO4J_PASSWORD": ""}):
            self.assertNotIn("wiki_pages", cc.probe_memory())


def _pending_skill(name="weekday_names", status="pending"):
    """A skill exactly as the forge stores one after its tests passed, without needing the sandbox."""
    from src.foundation import forge
    code = "def run(args):\n    return sorted(args.get('x', []))\n"
    tests = "import unittest\nfrom skill import run\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(run({'x': [2, 1]}), [1, 2])\n"
    forge._save({"name": name, "description": "sorts a list", "status": status, "uses": 0, "attempts": 2, "created": "2026-09-19T21:20:05-0600",
                 "sha256": forge.digest(code, tests), "test_result": {"ran": 4, "failures": 0, "errors": 0}}, code, tests)
    return forge


class DashboardDataTests(_Base):
    def test_skills_waiting_for_you_come_first_with_their_proof(self):
        _pending_skill("zeta_tool", "pending")
        _pending_skill("alpha_tool", "active")
        out = cc.probe_skills()
        self.assertEqual([s["name"] for s in out["items"]], ["zeta_tool", "alpha_tool"])       # pending before active, not alphabetical
        self.assertEqual((out["total"], out["active"], out["pending"]), (2, 1, 1))
        self.assertEqual((out["items"][0]["tests_ran"], out["items"][0]["attempts"]), (4, 2))
        self.assertEqual(out["items"][0]["created"], "2026-09-19 21:20")

    def test_tasks_lists_open_todos_by_due_date_and_live_projects(self):
        from src.foundation import projects
        reg = projects.Registry()
        reg.add_project("Bike rebuild - waiting on parts")
        reg.add_todo("order chain due 2026-10-01")
        reg.add_todo("call the bank due 2026-09-25")
        reg.add_todo("no due date at all")
        first = reg.load()["todos"][0].id
        reg.complete_todo(first)
        out = cc.probe_tasks(fetch_scheduled=lambda: [{"name": "Email check", "status": "active", "schedule": "daily", "next_run": "2026-09-20 07:00",
                                                        "last_run": "", "runs": 3}])
        self.assertEqual([t["text"] for t in out["todos"]], ["call the bank", "no due date at all"])   # dated first, undated last
        self.assertEqual([t["due"] for t in out["todos"]], ["2026-09-25", ""])
        self.assertNotIn(first, [t["id"] for t in out["todos"]])                             # completed ones are gone
        self.assertEqual(out["todo_total"], 2)
        self.assertEqual(out["projects"][0]["name"], "Bike rebuild")
        self.assertEqual(out["scheduled"][0]["name"], "Email check")

    def test_tasks_fail_soft_when_the_scheduler_database_is_unavailable(self):
        def boom():
            raise RuntimeError("db down")
        out = cc.probe_tasks(fetch_scheduled=boom)
        self.assertEqual(out["scheduled"], [])
        self.assertEqual(out["todos"], [])

    def test_services_rows_show_what_is_up_and_what_is_down(self):
        raw = {"telegram": {"configured": True, "listener_alive": True, "scheduler_alive": False, "in_flight": 1},
               "sandbox": {"available": True, "tools_present": 14, "tools_total": 19, "openhands": True},
               "endpoints": [{"name": "GPU box", "ok": True, "loaded": [{"name": "qwen2.5:7b", "vram_gb": 4.3}], "models": None},
                             {"name": "CPU box", "ok": False, "loaded": [], "models": None}]}
        rows = {r["name"]: r for r in cc.probe_services(raw, check=lambda host, port: host != "neo4j",
                                                         godseye_url="http://100.64.0.20:4173/")}
        self.assertTrue(rows["Telegram listener"]["ok"])
        self.assertFalse(rows["Standing scheduler"]["ok"])
        self.assertIn("14/19 tools", rows["Mission sandbox"]["detail"])
        self.assertIn("qwen2.5:7b (4.3 GB VRAM)", rows["Model: GPU box"]["detail"])
        self.assertFalse(rows["Model: CPU box"]["ok"])
        self.assertFalse(rows["Neo4j"]["ok"])
        self.assertEqual(rows["Neo4j"]["detail"], "not answering")
        self.assertTrue(rows["ChromaDB"]["ok"])
        for name in ("Odysseus app", "LLM gateway", "SearXNG", "ntfy", "God's Eye", "Sandbox gateway"):
            self.assertIn(name, rows)

    def test_peer_hosts_can_be_overridden_and_default_to_compose_service_names(self):
        seen = []
        with mock.patch.dict(os.environ, {"NEO4J_HOST": "graph.local"}):
            cc.probe_services({}, check=lambda host, port: seen.append((host, port)) or True)
        self.assertIn(("graph.local", 7687), seen)
        self.assertIn(("chromadb", 8000), seen)
        self.assertNotIn(("gods-eye", 4173), seen)                # its own isolated network: not resolvable from the app, so never checked by name

    def test_gods_eye_is_checked_where_the_owner_browses_and_left_out_when_unknown(self):
        seen = []
        rows = {r["name"]: r for r in cc.probe_services({}, check=lambda h, p: seen.append((h, p)) or (h == "100.64.0.20"),
                                                        godseye_url="http://100.64.0.20:4173/")}
        self.assertIn(("100.64.0.20", 4173), seen)
        self.assertTrue(rows["God's Eye"]["ok"])
        self.assertNotIn("God's Eye", [r["name"] for r in cc.probe_services({}, check=lambda h, p: True)])
        self.assertNotIn("God's Eye", [r["name"] for r in cc.probe_services({}, check=lambda h, p: True, godseye_url="not a url")])

    def test_server_vitals_come_from_the_health_probe_and_carry_the_thresholds(self):
        raw = {"system": {"cpu_pct": 3.0, "mem_pct": 47.3, "mem_gb": 7.7, "disk_pct": 96.0, "disk_free_gb": 8.8, "disk_total_gb": 234.0,
                          "disk_used_gb": 213.0, "uptime_h": 596.0, "load1": 0.4, "junk": "x", "psutil_error": None}}
        out = cc.server_stats(raw)
        self.assertEqual((out["disk_pct"], out["disk_free_gb"], out["disk_total_gb"]), (96.0, 8.8, 234.0))
        self.assertNotIn("junk", out)
        self.assertEqual(out["bad"]["disk_pct"], 92.0)                                        # same level the watchdog pages at
        from src.foundation import watchdog
        self.assertEqual(cc.SERVER_BAD["disk_pct"], watchdog.DISK_PCT_LIMIT)
        self.assertLess(out["warn"]["disk_pct"], out["bad"]["disk_pct"])
        self.assertEqual(cc.server_stats({})["warn"], cc.SERVER_WARN)                         # no probe data: still well-formed

    def test_snapshot_includes_the_server_block_and_survives_a_missing_probe(self):
        s = cc.snapshot(dict(SYS, system=lambda: {"cpu_pct": 5.0, "mem_pct": 40.0, "disk_pct": 50.0, "disk_free_gb": 100.0}), EXTRA)
        self.assertEqual(s["server"]["disk_free_gb"], 100.0)
        def boom():
            raise RuntimeError("psutil missing")
        self.assertIn("warn", cc.snapshot(dict(SYS, system=boom), EXTRA)["server"])

    def test_running_now_reports_the_active_mission_and_loaded_models(self):
        base = {"missions": [{"id": "m-1", "status": "completed"}, {"id": "m-2", "status": "active", "objective": "x"}],
                "standing": [{"status": "active"}, {"status": "paused"}]}
        raw = {"endpoints": [{"name": "gpu", "ok": True, "loaded": [{"name": "qwen", "vram_gb": 4.3}]}], "telegram": {"in_flight": 2}}
        out = cc.running_now(base, raw)
        self.assertEqual(out["mission"]["id"], "m-2")
        self.assertEqual(out["models"], [{"endpoint": "gpu", "model": "qwen", "vram_gb": 4.3}])
        self.assertEqual((out["in_flight"], out["standing_active"]), (2, 1))
        self.assertIsNone(cc.running_now({"missions": [], "standing": []}, {})["mission"])

    def test_snapshot_survives_a_broken_service_check_and_reports_it(self):
        def explode(host, port):
            raise RuntimeError("dns is down")
        s = cc.snapshot(SYS, EXTRA, service_check=explode)
        rows = {r["name"]: r for r in s["services"]}
        self.assertTrue(rows["Odysseus app"]["ok"])                                          # the card is still there...
        self.assertFalse(rows["Neo4j"]["ok"])                                                # ...and the broken check reads as "down"
        self.assertFalse(rows["ChromaDB"]["ok"])

    def test_probing_does_not_leak_endpoint_urls_into_the_systems_document(self):
        probes = dict(SYS, endpoints=lambda: [{"name": "gpu", "ok": True, "url": "http://100.64.0.10:11434", "loaded": [], "models": None}])
        doc = sysview.snapshot(probes)
        self.assertNotIn("100.64.0.10", json.dumps(doc))
        self.assertNotIn("probes", doc)
        raw = {}
        sysview.snapshot(probes, raw=raw)
        self.assertEqual(raw["endpoints"][0]["name"], "gpu")                                 # available to the Command Center only


class HomelabLinkTests(_Base):
    def test_default_watches_the_gpu_box_which_is_the_owners_windows_pc_and_not_the_retired_linux_node(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("AURIX_HOMELAB_LINKS", None)
            links = cc.parse_homelab_links()
        self.assertEqual(links[0]["url"], "http://100.64.0.10:11434")                   # this Windows PC's Ollama, on its Tailscale address
        self.assertEqual((links[0]["host"], links[0]["port"]), ("100.64.0.10", "11434"))
        self.assertIn("GPU box", links[0]["name"])
        self.assertTrue(links[0]["quiet"])                                                  # a gaming PC that sleeps must never page anyone
        self.assertFalse(any("100.64.0.30" in l["url"] for l in links))                   # the retired Linux install's node is gone
        self.assertFalse(any(l["port"] == "3000" for l in links))                           # ...and so is its Homepage dashboard
        by_name = {l["name"]: l for l in links}
        self.assertEqual((by_name["Jellyfin (Pi)"]["host"], by_name["Jellyfin (Pi)"]["port"]), ("10.0.0.75", "8096"))
        self.assertEqual((by_name["Pi-hole (Pi)"]["host"], by_name["Pi-hole (Pi)"]["port"]), ("10.0.0.75", "80"))
        self.assertEqual(by_name["Vaultwarden (server)"]["port"], "8080")
        self.assertEqual(by_name["Uptime Kuma (server)"]["port"], "3001")
        self.assertEqual(len(links), 5)

    def test_env_override_supports_several_links_default_ports_and_a_cap(self):
        spec = "Jellyfin|http://10.0.0.75:8096; Vault|https://vault.local ;Plain|http://box.local"
        got = {l["name"]: l for l in cc.parse_homelab_links(spec)}
        self.assertEqual(got["Jellyfin"]["port"], "8096")
        self.assertEqual(got["Vault"]["port"], "443")
        self.assertEqual(got["Plain"]["port"], "80")
        many = ";".join(f"S{i}|http://h{i}:{3000 + i}" for i in range(30))
        self.assertEqual(len(cc.parse_homelab_links(many)), cc.MAX_HOMELAB_LINKS)
        with mock.patch.dict(os.environ, {"AURIX_HOMELAB_LINKS": "Only|http://x:1"}):
            self.assertEqual([l["name"] for l in cc.parse_homelab_links()], ["Only"])
        with mock.patch.dict(os.environ, {"AURIX_HOMELAB_LINKS": ""}):
            self.assertEqual(cc.parse_homelab_links(), [])                        # empty means "none", not "the default"

    def test_only_plain_http_urls_become_links(self):
        bad = ["X|javascript:alert(1)", "X|data:text/html,<script>1</script>", "X|ftp://host:21", "X|http://", "X|http://h:99999",
               "X|http://h:port", "|http://host:3000", "no-separator http://host:3000", "X|http://host:3000/a b", 'X|http://h:1/"onmouseover=x',
               "X|http://h:1/<b>", "X|file:///etc/passwd", "X|//host:3000", "X|  "]
        for spec in bad:
            self.assertEqual(cc.parse_homelab_links(spec), [], spec)

    def test_reachability_and_the_quiet_flag(self):
        links = cc.parse_homelab_links("Gaming PC|http://100.64.0.10:11434|quiet;Other|http://other:80;Loud|http://loud:80|")
        self.assertEqual([l["quiet"] for l in links], [True, False, False])
        rows = {r["name"]: r for r in cc.probe_homelab(check=lambda host, port: host == "other", links=links)}
        self.assertFalse(rows["Gaming PC"]["ok"])
        self.assertTrue(rows["Gaming PC"]["quiet"])
        self.assertIn("not alerted", rows["Gaming PC"]["detail"])
        self.assertEqual(rows["Loud"]["detail"], "not answering")                            # a normal link says nothing about alerts
        self.assertFalse(rows["Loud"]["quiet"])
        self.assertTrue(rows["Other"]["ok"])
        self.assertEqual(rows["Other"]["detail"], "answering")
        self.assertEqual(rows["Other"]["url"], "http://other:80")

    def test_only_the_exact_word_quiet_silences_a_link(self):
        for flag in ("Quiet ", "QUIET"):
            self.assertTrue(cc.parse_homelab_links(f"A|http://a:1|{flag}")[0]["quiet"], flag)
        for flag in ("quiet2", "silent", "true", "1", "quiet|x"):
            self.assertFalse(cc.parse_homelab_links(f"A|http://a:1|{flag}")[0]["quiet"], flag)

    def test_a_check_that_raises_reads_as_down(self):
        def boom(host, port):
            raise OSError("no route")
        rows = cc.probe_homelab(check=boom, links=cc.parse_homelab_links("A|http://a:1"))
        self.assertEqual([r["ok"] for r in rows], [False])

    def test_an_offline_peer_is_probed_at_most_once_per_cache_window(self):
        calls = []
        with mock.patch.object(cc, "tcp_up", lambda host, port, timeout=1.0: calls.append((host, port)) or False):
            with mock.patch.dict(os.environ, {"AURIX_HOMELAB_LINKS": "Off|http://100.64.0.30:3000"}):
                for _ in range(5):
                    cc.probe_homelab()
                self.assertEqual(calls.count(("100.64.0.30", 3000)), 1)
                with mock.patch.object(cc.time, "time", return_value=time_now() + 60):
                    cc.probe_homelab()
                self.assertEqual(calls.count(("100.64.0.30", 3000)), 2)              # window over: probe again

    def test_snapshot_carries_the_homelab_links(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("AURIX_HOMELAB_LINKS", None)
            s = cc.snapshot(SYS, EXTRA)
        self.assertEqual(s["homelab"][0]["url"], "http://100.64.0.10:11434")
        self.assertTrue(s["homelab"][0]["ok"])                                        # the patched network says everything answers


def time_now():
    import time
    return time.time()


class SkillActionTests(_Base):
    def test_detail_shows_code_tests_and_integrity(self):
        _pending_skill()
        d = cc.skill_detail("weekday_names")
        self.assertIn("def run(args)", d["code"])
        self.assertIn("class T", d["tests"])
        self.assertTrue(d["integrity_ok"])
        self.assertEqual(d["status"], "pending")
        self.assertIsNone(cc.skill_detail("nope_nothing"))
        self.assertIsNone(cc.skill_detail("../etc"))

    def test_approve_deny_retire_go_through_the_forges_own_rules(self):
        _pending_skill("a_skill")
        _pending_skill("b_skill")
        ok = cc.skill_action("a_skill", "approve")
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["status"], "active")
        self.assertNotIn("<", ok["message"])                                                 # plain text for the toast
        self.assertTrue(cc.skill_action("b_skill", "deny")["ok"])
        self.assertFalse(cc.skill_action("b_skill", "approve")["ok"])                        # a denied skill can never be approved
        self.assertTrue(cc.skill_action("a_skill", "retire")["ok"])
        self.assertFalse(cc.skill_action("a_skill", "approve")["ok"])
        events = [json.loads(line)["event"] for line in open(audit.audit_path(), encoding="utf-8")]
        for e in ("skill_forged", "skill_denied", "skill_retired"):
            self.assertIn(e, events)

    def test_a_tampered_skill_cannot_be_approved_from_the_dashboard(self):
        forge = _pending_skill()
        forge.skill_dir("weekday_names").joinpath("skill.py").write_text("def run(args):\n    return 'pwned'\n", encoding="utf-8")
        res = cc.skill_action("weekday_names", "approve")
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "pending")
        self.assertIn("changed after its tests passed", res["message"])

    def test_bad_names_and_verbs_are_refused_without_touching_anything(self):
        _pending_skill()
        for name, verb in (("../x", "approve"), ("weekday_names", "delete"), ("weekday_names", ""), ("", "approve"), ("A", "deny")):
            self.assertFalse(cc.skill_action(name, verb)["ok"], (name, verb))
        self.assertEqual(cc.skill_detail("weekday_names")["status"], "pending")


class DashboardRouteTests(unittest.TestCase):
    """fastapi is not installed on the dev PC, so the route guards are checked from the source (same approach as the approvals route)."""

    def setUp(self):
        self.src = open(os.path.join(ROOT, "routes", "command_routes.py"), encoding="utf-8").read()

    def block(self, marker):
        start = self.src.index(marker)
        nxt = self.src.find("    @router.", start + 10)
        return self.src[start: nxt if nxt != -1 else len(self.src)]

    def test_skill_decisions_require_a_real_human_admin_and_the_confirm_header(self):
        post = self.block('@router.post("/api/command/skills/{name}/{verb}")')
        code = post[post.index("async def skill_decide"):]
        guard = code.index("_require_human_admin(request)")
        self.assertLess(guard, code.index('headers.get("X-AURIX-Confirm"'))       # the identity check comes before anything else
        self.assertLess(guard, code.index("command_center.skill_action"))            # ...and before the action runs
        self.assertNotIn("dependencies=[Depends(require_admin)]", post.split("async def")[0])   # the generic admin check also admits the agent
        self.assertIn("verb.upper()", code)
        self.assertIn("command_center.SKILL_VERBS", code)

    def test_reading_a_skill_needs_an_admin_and_404s_when_missing(self):
        get = self.block('@router.get("/api/command/skills/{name}"')
        self.assertIn("Depends(require_admin)", get)
        self.assertIn("404", get)

    def test_dashboard_is_an_alias_of_the_command_page_and_still_admin_only(self):
        decorators = self.src[self.src.index('@router.get("/command"'):self.src.index("async def page")]
        self.assertIn('@router.get("/dashboard"', decorators)                       # stacked on the same handler
        self.assertIn("require_admin(request)", self.src[self.src.index("async def page"):self.src.index('@router.get("/api/command"')])

    def test_links_include_uptime_kuma_on_the_same_host(self):
        self.assertIn("KUMA_PORT", self.src)
        self.assertIn("_kuma_url(request)", self.src)


class ApprovalAuthorityTests(unittest.TestCase):
    """An agent must never be able to approve its own request through the web API."""

    def test_truth_table(self):
        ok = cc.human_admin_ok
        self.assertTrue(ok(False, "rascal", True, True))
        self.assertFalse(ok(True, "rascal", True, True))                      # internal loopback token present
        self.assertFalse(ok(False, "internal-tool", True, True))              # stamped by the middleware for the agent's tools
        self.assertFalse(ok(False, None, True, True))                         # anonymous
        self.assertFalse(ok(False, "", True, True))
        self.assertFalse(ok(False, "rascal", False, True))                    # auth disabled/unconfigured: no identity to trust
        self.assertFalse(ok(False, "rascal", True, False))                    # logged in but not an admin
        self.assertFalse(ok(True, "internal-tool", False, False))

    def test_only_one_combination_passes(self):
        import itertools
        passing = [c for c in itertools.product([False, True], ["rascal", "internal-tool", None], [False, True], [False, True])
                   if cc.human_admin_ok(*c)]
        self.assertEqual(passing, [(False, "rascal", True, True)])

    def test_decision_header(self):
        self.assertEqual(cc.parse_decision("APPROVE"), "approve")
        self.assertEqual(cc.parse_decision(" deny "), "deny")
        for bad in ("", "yes", "approve all", "STOP", None, "APPROVE;DENY"):
            self.assertIsNone(cc.parse_decision(bad), bad)

    def test_approval_id_shape(self):
        for good in ("abc123", "000000", "ffffff"):
            self.assertTrue(cc.APPROVAL_ID_RE.match(good))
        for bad in ("all", "ABC123", "abc12", "abc1234", "../etc", "abc12g", "abc123\n"):
            self.assertIsNone(cc.APPROVAL_ID_RE.match(bad), bad)

    def test_the_route_uses_the_strict_check_not_the_generic_admin_dependency(self):
        src = open(os.path.join(ROOT, "routes", "command_routes.py"), encoding="utf-8").read()
        decide = src[src.index("async def decide"):]
        self.assertIn("_require_human_admin(request)", decide.split("verb =")[0])
        self.assertNotIn("dependencies=[Depends(require_admin)]", src[src.index('@router.post("/api/command/approvals'):][:120])


class PageTests(unittest.TestCase):
    PAGE = os.path.join(ROOT, "static", "command.html")

    def src(self):
        return open(self.PAGE, encoding="utf-8").read()

    def test_every_script_is_allowed_by_the_apps_csp(self):
        src = self.src()
        tags = re.findall(r"<script\b[^>]*>", src)
        self.assertTrue(tags)
        for tag in tags:
            self.assertTrue('nonce="{{CSP_NONCE}}"' in tag or " src=" in tag, tag)
        self.assertNotIn("{{CSP_NONCE}}", sysview.render_page(src, "n0nce"))

    def test_no_innerhtml_and_only_same_origin_requests(self):
        src = self.src()
        code = src.split("<script", 1)[1]
        for bad in (".innerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(bad, code)
        self.assertEqual(re.findall(r"""(?:src|href|action)=["']https?://""", src), [])
        for url in re.findall(r"""fetch\(\s*["']([^"']+)""", src):
            self.assertTrue(url.startswith("/"), url)

    def test_approval_buttons_send_the_confirm_header(self):
        code = self.src()
        self.assertIn('"APPROVE"', code)
        self.assertIn('"DENY"', code)
        self.assertIn("X-AURIX-Confirm", code)
        self.assertIn("/api/systems/stop", code)

    def test_uses_the_odysseus_theme(self):
        self.assertIn("odysseus-theme", self.src())

    def test_the_page_has_every_card_the_owner_asked_for(self):
        src = self.src()
        for element_id in ("skills", "running", "sched", "todos", "projects", "services", "missions", "standing", "organs",
                           "l-godseye", "l-kuma", "skill-nudge", "approvals"):
            self.assertIn(f'id="{element_id}"', src, element_id)
        for label in ("Odysseus chat", "God's Eye View", "Uptime Kuma", "Running now", "Tasks &amp; projects", "Services"):
            self.assertIn(label, src, label)

    def test_server_card_ranks_severity_properly_and_is_wired(self):
        src = self.src()
        self.assertIn('id="server"', src)
        body = src[src.index("function drawServer"):src.index("function drawHomelab")]
        self.assertIn('rank = { "": 0, warn: 1, bad: 2 }', body)                             # an alphabetical sort would put "warn" above "bad"
        self.assertNotIn(".sort()", body)
        self.assertNotIn("innerHTML", body)
        self.assertIn('changed("server", state.server)', src)

    def test_homelab_links_are_scheme_checked_in_the_page_too(self):
        src = self.src()
        self.assertIn('id="homelab"', src)
        body = src[src.index("function drawHomelab"):src.index("function drawServices")]
        self.assertIn(r"/^https?:\/\//i.test(", body)                                  # never javascript:/data: even if the API were tampered with
        self.assertIn('rel: "noopener noreferrer"', body)
        self.assertIn('target: "_blank"', body)
        self.assertNotIn("innerHTML", body)

    def test_skill_buttons_send_the_confirm_header_and_ask_first(self):
        src = self.src()
        for verb in ('"APPROVE"', '"DENY"', '"RETIRE"'):
            self.assertIn(verb, src)
        self.assertIn('"/api/command/skills/" + encodeURIComponent(name)', src)
        body = src[src.index("async function skillDecide"):src.index("async function viewSkill")]
        self.assertIn("confirm(", body)
        self.assertIn('"X-AURIX-Confirm": verb', body)
        self.assertIn('method: "POST"', body)

    def test_model_written_skill_text_only_reaches_the_page_as_text(self):
        # names, descriptions and code are model-written: they must go through textContent, never markup
        code = self.src().split("<script", 1)[1]
        view = code[code.index("async function viewSkill"):code.index("function drawSkills")]
        self.assertIn('el("div", { class: "code" }, d.code)', view)
        self.assertNotIn("innerHTML", code)

    def test_rail_buttons_exist_and_are_wired(self):
        index = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
        js = open(os.path.join(ROOT, "static", "js", "aurix-rail.js"), encoding="utf-8").read()
        for rail_id in ("rail-command", "rail-godseye"):
            self.assertIn(f'id="{rail_id}"', index)
            self.assertIn(rail_id, js)
        self.assertIn("/static/js/aurix-rail.js", index)
        self.assertLess(index.index("aurix-rail.js"), index.index('src="/static/js/init.js"'))


if __name__ == "__main__":
    unittest.main()
