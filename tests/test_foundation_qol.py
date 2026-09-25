"""Owner quality-of-life commands, the projects/to-do registry, file delivery, Telegram send_document."""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import approval_gate as ag  # noqa: E402
from src.foundation import audit, commands, heartbeat, mission as ms, projects, qol  # noqa: E402

GRANTED = dict(env={}, which=lambda b: None, find_spec=lambda m: None, probe=lambda h, p: False, authorizations={})


class ParseTests(unittest.TestCase):
    def test_new_commands_parse(self):
        p = commands.parse
        cases = {
            "help": ("help", ""), "/help": ("help", ""), "?": ("help", ""), "history": ("history", ""),
            "show m-abc123": ("show", "m-abc123"), "SHOW SM-ABC123": ("show", "sm-abc123"),
            "approvals": ("approvals", ""), "log": ("log", ""), "log 20": ("log", "20"),
            "files": ("files", ""), "files m-abc123": ("files", "m-abc123"),
            "send files": ("send_files", ""), "send files m-abc123": ("send_files", "m-abc123"),
            "tools": ("tools", ""), "doctor": ("doctor", ""), "health": ("doctor", ""), "ping": ("ping", ""),
            "authorizations": ("authorizations", ""), "projects": ("projects", ""), "todos": ("todos", ""),
            "project: Bike rebuild - waiting on parts": ("project_new", "Bike rebuild - waiting on parts"),
            "project note p-abc123 Parts Arrived": ("project_act", "note p-abc123 Parts Arrived"),
            "project DONE P-ABC123": ("project_act", "done p-abc123"),
            "todo: call the machinist due 2026-10-01": ("todo_new", "call the machinist due 2026-10-01"),
            "done t-abc123": ("todo_done", "t-abc123"),
        }
        for text, want in cases.items():
            self.assertEqual(p(text), want, text)

    def test_ordinary_chat_is_not_swallowed(self):
        for text in ("help me write a poem", "what is the log of 100", "show me the money", "done", "files are here",
                     "project", "todo", "ping the server please", "tools for woodworking", "done t-zzzzzz",
                     "history of rome", "show m-12345"):
            self.assertIsNone(commands.parse(text), text)

    def test_existing_commands_unchanged(self):
        p = commands.parse
        self.assertEqual(p("status"), ("status", ""))
        self.assertEqual(p("stop"), ("stop", ""))
        self.assertEqual(p("mission: do a thing"), ("new", "do a thing"))
        self.assertEqual(p("authorize content-rights 2026-12-01"), ("authorize", "content-rights 2026-12-01"))
        self.assertEqual(p("approve mission m-abc123"), ("approve_mission", "m-abc123"))


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name, "AURIX_TZ_OFFSET_MINUTES": "0"})
        self.env.start()
        ag.reset_state()
        self.sent = []

        async def notify(t):
            pass

        async def agent(p):
            return "STEP DONE: ok"

        async def send_file(path, caption=""):
            self.sent.append((os.path.basename(path), caption))
            return True

        self.f = commands.Foundation(agent, notify, llm=None, session_id="tg", send_file=send_file, **GRANTED)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
        ag.reset_state()

    def _mission(self, mid="m-abc123", status="completed", steps=2):
        m = self.f.store.propose(ms.MissionContract(id=mid, objective="convert a skull CT into an STL",
                                                     steps=[ms.Step(id=f"s{i}", title=f"step {i}") for i in range(steps)]))
        if status != "proposed":
            m = self.f.store.load(mid)
            m.status = ms.MissionStatus(status)
            self.f.store.save(m)
        return self.f.store.load(mid)

    def _ws(self, m):
        ws = Path(m.workspace)
        ws.mkdir(parents=True, exist_ok=True)
        return ws


class ReadCommandTests(_Base):
    async def test_help_lists_the_commands(self):
        h = await self.f.handle("help")
        for word in ("mission:", "stop", "doctor", "send files", "todo:", "projects", "/systems"):
            self.assertIn(word, h)

    async def test_ping(self):
        self.assertTrue((await self.f.handle("ping")).startswith("pong"))

    async def test_history_and_show(self):
        self.assertIn("No missions yet", await self.f.handle("history"))
        self._mission()
        h = await self.f.handle("history")
        self.assertIn("m-abc123", h)
        self.assertIn("completed", h)
        s = await self.f.handle("show", "m-abc123")
        self.assertIn("step 0", s)
        self.assertIn("convert a skull", s)
        self.assertIn("No mission m-ffffff", await self.f.handle("show", "m-ffffff"))
        self.assertIn("No standing mission sm-ffffff", await self.f.handle("show", "sm-ffffff"))

    async def test_show_escapes_html_in_model_text(self):
        self.f.store.propose(ms.MissionContract(id="m-bad000", objective="<script>alert(1)</script> & more"))
        s = await self.f.handle("show", "m-bad000")
        self.assertNotIn("<script>", s)
        self.assertIn("&lt;script&gt;", s)

    async def test_approvals_empty_and_listed(self):
        self.assertIn("Nothing is waiting", await self.f.handle("approvals"))
        loop = asyncio.get_running_loop()
        ag._pending["abc123"] = ag.PendingApproval(id="abc123", tool="bash", preview="pip install <x>", session_id="s",
                                                    owner="o", created=time.time() - 5, future=loop.create_future())
        t = await self.f.handle("approvals")
        self.assertIn("abc123", t)
        self.assertIn("pip install &lt;x&gt;", t)

    async def test_log(self):
        self.assertIn("empty", await self.f.handle("log"))
        for i in range(30):
            audit.append("tick", note=f"n{i}")
        self.assertEqual((await self.f.handle("log", "5")).count("<b>tick</b>"), 5)
        self.assertEqual((await self.f.handle("log", "99")).count("<b>tick</b>"), 30)    # capped at 40, only 30 exist

    async def test_tools_and_authorizations(self):
        t = await self.f.handle("tools")
        self.assertIn("What I can use", t)
        self.assertIn("▫️", t)
        a = await self.f.handle("authorizations")
        self.assertIn("live-trading", a)
        self.assertIn("not available yet", a)
        await self.f.handle("authorize", "content-rights")
        self.assertIn("✅ <code>content-rights</code>", await self.f.handle("authorizations"))

    async def test_doctor_summarises_every_organ(self):
        fake = {"system": lambda: {"cpu_pct": 1.0}, "endpoints": lambda: [], "sandbox": lambda: {"available": False},
                "telegram": lambda: {}, "governor": lambda: {"mode": "n/a"}}
        from src.foundation import sysview
        with mock.patch.object(sysview, "live_probes", return_value=fake):
            d = await self.f.handle("doctor")
        self.assertIn("AURIX doctor", d)
        self.assertIn("need a look", d)                       # sandbox unreachable -> warn
        for title in ("Immune system", "Brain", "Muscles"):
            self.assertIn(title, d)


class ProjectTests(_Base):
    async def test_project_lifecycle(self):
        self.assertIn("No open projects", await self.f.handle("projects"))
        r = await self.f.handle("project_new", "Bike rebuild - waiting on parts")
        pid = self.f.registry.load()["projects"][0].id
        self.assertIn(pid, r)
        self.assertIn("already an open project", await self.f.handle("project_new", "bike REBUILD"))
        self.assertIn("waiting on parts", await self.f.handle("projects"))
        await self.f.handle("project_act", f"note {pid} Parts Arrived")
        self.assertEqual(self.f.registry.load()["projects"][0].note, "Parts Arrived")
        await self.f.handle("project_act", f"pause {pid}")
        self.assertEqual(self.f.registry.load()["projects"][0].status, "paused")
        await self.f.handle("project_act", f"done {pid}")
        self.assertIn("No open projects", await self.f.handle("projects"))
        self.assertIn("No project p-ffffff", await self.f.handle("project_act", "done p-ffffff"))

    async def test_project_needs_a_name_and_note_needs_text(self):
        self.assertIn("name", await self.f.handle("project_new", " - just a note"))
        await self.f.handle("project_new", "Thing")
        pid = self.f.registry.load()["projects"][0].id
        self.assertIn("Say what to note", await self.f.handle("project_act", f"note {pid}"))

    async def test_a_long_free_form_message_keeps_every_word(self):
        # the exact shape of the 2026-09-20 incident: no " - " and longer than the 80-character name limit. It used to become a
        # name clipped at 80 characters with an EMPTY note, silently losing everything after that.
        msg = ("adventure E bike built from mountain bike base able to climb Mount Rosalie as a benchmark climb, "
               "electric assist, frame reused from the old hardtail")
        await self.f.handle("project_new", msg)
        p = self.f.registry.load()["projects"][0]
        self.assertLessEqual(len(p.name), 80)
        self.assertTrue(p.name.endswith("..."))
        self.assertEqual(p.note, msg)                                        # nothing dropped
        stem = p.name[:-3]
        self.assertTrue(msg.startswith(stem))
        self.assertEqual(msg[len(stem)], " ", "the name is cut at a word boundary, not mid-word")

    async def test_a_short_first_sentence_becomes_the_name(self):
        msg = "Build a bike. It should climb hills and carry gear for two days of riding in the mountains near home base"
        await self.f.handle("project_new", msg)
        p = self.f.registry.load()["projects"][0]
        self.assertEqual((p.name, p.note), ("Build a bike", msg))

    async def test_the_name_dash_note_form_is_unchanged_and_short_free_text_is_just_a_name(self):
        await self.f.handle("project_new", "Bike rebuild - waiting on parts")
        await self.f.handle("project_new", "Spider-Man port")
        a, b = self.f.registry.load()["projects"]
        self.assertEqual((a.name, a.note), ("Bike rebuild", "waiting on parts"))
        self.assertEqual((b.name, b.note), ("Spider-Man port", ""))
        self.assertEqual(len(self.f.registry.load()["projects"]), 2)

    async def test_a_long_name_before_a_dash_keeps_both_parts(self):
        head = "port the Spider-Man game to the PlayStation Vita as a demake with pure emulation of the hardware"
        await self.f.handle("project_new", f"{head} - not started")
        p = self.f.registry.load()["projects"][0]
        self.assertLessEqual(len(p.name), 80)
        self.assertEqual(p.note, f"{head} - not started")

    async def test_nothing_is_ever_cut_silently(self):
        await self.f.handle("project_new", "Huge - " + "word " * 300)                      # far over the limit
        p = self.f.registry.load()["projects"][0]
        self.assertLessEqual(len(p.note), projects.MAX_NOTE)
        self.assertTrue(p.note.endswith("..."), "a cut note must SAY it was cut")
        await self.f.handle("project_act", f"note {p.id} " + "x " * 600)
        self.assertTrue(self.f.registry.load()["projects"][0].note.endswith("..."))
        await self.f.handle("todo_new", "y " * 400)
        self.assertTrue(self.f.registry.load()["todos"][0].text.endswith("..."))

    async def test_untouched_text_gets_no_marker(self):
        await self.f.handle("project_new", "Thing - " + "z" * 700)
        self.assertFalse(self.f.registry.load()["projects"][0].note.endswith("..."))

    async def test_todos_due_project_and_done(self):
        await self.f.handle("project_new", "Bike")
        pid = self.f.registry.load()["projects"][0].id
        await self.f.handle("todo_new", "call machinist due 2026-10-01")
        await self.f.handle("todo_new", f"order chain for {pid}")
        await self.f.handle("todo_new", "ancient chore due 2020-01-01")
        todos = self.f.registry.load()["todos"]
        self.assertEqual({t.text for t in todos}, {"call machinist", "order chain", "ancient chore"})
        self.assertEqual(next(t for t in todos if t.text == "order chain").project, pid)
        listing = await self.f.handle("todos")
        self.assertIn("OVERDUE 2020-01-01", listing)
        self.assertLess(listing.index("ancient chore"), listing.index("call machinist"))       # soonest due first
        self.assertIn("1 open to-do", await self.f.handle("projects"))
        tid = next(t.id for t in todos if t.text == "call machinist")
        self.assertIn("Done", await self.f.handle("todo_done", tid))
        self.assertIn("already done", await self.f.handle("todo_done", tid))
        self.assertNotIn("call machinist", await self.f.handle("todos"))
        self.assertIn("No to-do t-ffffff", await self.f.handle("todo_done", "t-ffffff"))

    async def test_todo_validation(self):
        self.assertIn("not valid", await self.f.handle("todo_new", "x due 2026-13-45"))
        self.assertIn("No project p-ffffff", await self.f.handle("todo_new", "x for p-ffffff"))
        self.assertIn("What is the to-do", await self.f.handle("todo_new", " due 2026-10-01"))

    async def test_stale_projects_and_digest_line(self):
        r = projects.Registry()
        r.add_project("Old thing")
        data = r.load()
        data["projects"][0].touched = time.time() - 30 * 86400
        r.save(data)
        self.assertIn("stale", r.render_projects())
        r.add_todo("late thing due 2020-01-01")
        line = r.digest_line()
        self.assertIn("1 overdue", line)
        self.assertIn("1 stale project: Old thing", line)
        self.assertIn("Yours:", heartbeat.overnight_digest(self.f.store))

    async def test_digest_line_empty_when_nothing(self):
        self.assertEqual(projects.Registry().digest_line(), "")

    async def test_html_is_escaped(self):
        await self.f.handle("project_new", "<b>x</b> - <i>y</i>")
        out = await self.f.handle("projects")
        self.assertNotIn("<b>x</b>", out)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", out)

    async def test_corrupt_registry_is_survivable(self):
        p = projects.registry_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not json", encoding="utf-8")
        self.assertIn("No open projects", await self.f.handle("projects"))
        self.assertIn("Added", await self.f.handle("project_new", "Recovered"))


class FileTests(_Base):
    async def test_no_workspace_and_empty(self):
        self.assertIn("No missions yet", await self.f.handle("files"))
        self._mission()
        self.assertIn("Nothing produced", await self.f.handle("files", "m-abc123"))

    async def test_lists_and_delivers_only_safe_files(self):
        m = self._mission()
        ws = self._ws(m)
        (ws / "skull.stl").write_bytes(b"solid x" * 100)
        (ws / "notes").mkdir()
        (ws / "notes" / "quote.txt").write_text("cheapest: $12", encoding="utf-8")
        (ws / ".env").write_text("SECRET=1", encoding="utf-8")
        (ws / "id_rsa").write_text("key", encoding="utf-8")
        (ws / "credentials.json").write_text("{}", encoding="utf-8")
        (ws / "empty.txt").write_text("", encoding="utf-8")
        (ws / ".hidden").mkdir()
        (ws / ".hidden" / "x.txt").write_text("x", encoding="utf-8")
        listing = await self.f.handle("files", "m-abc123")
        self.assertIn("skull.stl", listing)
        self.assertIn("quote.txt", listing)
        bullets = [l for l in listing.splitlines() if l.startswith("•")]
        for banned in (".env", "id_rsa", "credentials.json", "empty.txt", "x.txt"):
            self.assertFalse(any(banned in l for l in bullets), banned)      # secret-looking names may be *noted*, never listed
        self.assertIn("skipped id_rsa (protected/secret)", listing)
        out = await self.f.handle("send_files", "m-abc123")
        self.assertIn("Sent 2 file(s)", out)
        self.assertEqual({n for n, _ in self.sent}, {"skull.stl", "quote.txt"})
        self.assertTrue(any(r["event"] == "files_delivered" for r in audit.recent(5)))

    async def test_symlinks_are_never_followed(self):
        m = self._mission()
        ws = self._ws(m)
        outside = Path(self.tmp.name) / "outside_secret.txt"
        outside.write_text("do not send", encoding="utf-8")
        try:
            os.symlink(outside, ws / "innocent.txt")
        except (OSError, NotImplementedError):
            self.skipTest("cannot create symlinks on this machine")
        _, files, _ = qol.collect_files(self.f.store, m.id)
        self.assertEqual(files, [])

    async def test_size_and_count_caps(self):
        m = self._mission()
        ws = self._ws(m)
        for i in range(14):
            (ws / f"f{i:02d}.txt").write_text(f"x{i}", encoding="utf-8")
        with mock.patch.object(qol, "MAX_FILE_BYTES", 10):
            (ws / "big.bin").write_bytes(b"0" * 50)
            _, files, notes = qol.collect_files(self.f.store, m.id)
        self.assertEqual(len(files), qol.MAX_FILES)
        self.assertTrue(any("big.bin" in n and "too large" in n for n in notes))
        self.assertNotIn("big.bin", [p.name for p in files])
        self.assertTrue(any("older file" in n for n in notes))

    async def test_defaults_to_active_then_latest_mission(self):
        a = self._mission("m-aaaaaa", "completed")
        (self._ws(a) / "a.txt").write_text("a", encoding="utf-8")
        time.sleep(0.01)
        b = self._mission("m-bbbbbb", "completed")
        (self._ws(b) / "b.txt").write_text("b", encoding="utf-8")
        self.assertIn("b.txt", await self.f.handle("files"))            # latest
        self.assertIn("a.txt", await self.f.handle("files", "m-aaaaaa"))

    async def test_delivery_failure_is_reported(self):
        m = self._mission()
        (self._ws(m) / "a.txt").write_text("a", encoding="utf-8")

        async def broken(path, caption=""):
            return False
        self.f.send_file = broken
        self.assertIn("Could not send: a.txt", await self.f.handle("send_files"))
        self.f.send_file = None
        self.assertIn("not available", await self.f.handle("send_files"))


def _load_service():
    """services/telegram/__init__ pulls in httpx (absent on the dev PC); the service module itself is stdlib-only."""
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "services", "telegram", "service.py")
    spec = importlib.util.spec_from_file_location("telegram_service_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.TelegramService


class SendDocumentTests(unittest.TestCase):
    def test_multipart_upload_reaches_api(self):
        seen = {}

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers["Content-Length"])
                seen["path"], seen["ctype"], seen["body"] = self.path, self.headers["Content-Type"], self.rfile.read(n)
                out = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        TelegramService = _load_service()
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "my model (v2).stl"
            f.write_bytes(b"BINARYDATA")
            svc = TelegramService(bot_token="123:abc", chat_id="42", fallback_dir=Path(d) / "fb")
            svc.enabled = True
            svc.API_BASE = f"http://127.0.0.1:{srv.server_port}"
            res = svc.send_document(f, "hello")
            svc.MAX_DOCUMENT_BYTES = 3
            too_big = svc.send_document(f)
        srv.shutdown()
        srv.server_close()
        self.assertTrue(res.ok, res.error)
        self.assertEqual(seen["path"], "/bot123:abc/sendDocument")
        self.assertIn("multipart/form-data; boundary=", seen["ctype"])
        self.assertIn(b'name="chat_id"\r\n\r\n42', seen["body"])
        self.assertIn(b"BINARYDATA", seen["body"])
        self.assertIn(b'filename="my_model__v2_.stl"', seen["body"])     # filename sanitised
        self.assertFalse(too_big.ok)
        self.assertIn("too large", too_big.error)

    def test_missing_file_and_no_token(self):
        TelegramService = _load_service()
        svc = TelegramService(bot_token="", chat_id="")
        svc.enabled = True
        self.assertEqual(svc.send_document("nope.txt").error, "no_token")
        svc.bot_token, svc.chat_id = "t", "c"
        self.assertIn("unreadable", svc.send_document("definitely/missing.txt").error)


class SendMessageTests(unittest.TestCase):
    """TelegramService.send against a fake Bot API: splitting, plain-text retry on markup errors, file fallback."""

    def _server(self, reject_markup=False, always_fail=False):
        got = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                got.append(body)
                bad = always_fail or (reject_markup and body.get("parse_mode") and "<" in body["text"].replace("<b>", "").replace("</b>", ""))
                out = json.dumps({"ok": False, "description": "Bad Request: can't parse entities: Unsupported start tag"}
                                 if bad else {"ok": True}).encode()
                self.send_response(400 if bad else 200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        svc = _load_service()(bot_token="1:x", chat_id="42", fallback_dir=Path(d.name))
        svc.enabled = True
        svc.API_BASE = f"http://127.0.0.1:{srv.server_port}"
        return svc, got, Path(d.name)

    def test_short_message_is_one_call(self):
        svc, got, _ = self._server()
        self.assertTrue(svc.send("<b>hi</b>").ok)
        self.assertEqual([g["text"] for g in got], ["<b>hi</b>"])
        self.assertEqual(got[0]["parse_mode"], "HTML")

    def test_long_message_is_split_on_lines_and_nothing_is_lost(self):
        svc, got, _ = self._server()
        text = "\n".join(f"line {i:04d} " + "x" * 60 for i in range(200))         # ~14 KB
        self.assertTrue(svc.send(text).ok)
        self.assertGreater(len(got), 2)
        self.assertTrue(all(len(g["text"]) <= 4096 for g in got))
        self.assertTrue(got[0]["text"].startswith("(1/%d)" % len(got)))
        sent = "\n".join(g["text"].split("\n", 1)[1] for g in got)
        self.assertEqual(sent, text)

    def test_one_enormous_line_is_hard_split(self):
        svc, got, _ = self._server()
        self.assertTrue(svc.send("y" * 10000).ok)
        self.assertEqual(sum(len(g["text"].split("\n", 1)[1]) for g in got), 10000)

    def test_markup_rejection_retries_as_plain_text(self):
        svc, got, d = self._server(reject_markup=True)
        r = svc.send("<b>result</b>: if a < b and x <y> then ok")
        self.assertTrue(r.ok)
        self.assertEqual(len(got), 2)
        self.assertNotIn("parse_mode", got[1])
        self.assertEqual(got[1]["text"], "result: if a < b and x <y> then ok")     # only Telegram's own tags are stripped
        self.assertEqual(list(d.glob("*.txt")), [])                              # nothing fell back to a file


    def test_reply_markup_rides_on_the_last_chunk_and_survives_plain_text_retry(self):
        svc, got, _ = self._server()
        mk = {"inline_keyboard": [[{"text": "ok", "callback_data": "approve:abc123"}]]}
        long_text = "\n".join("z" * 100 for _ in range(120))                            # 12 KB -> several chunks
        self.assertTrue(svc.send(long_text, reply_markup=mk).ok)
        self.assertGreater(len(got), 1)
        self.assertTrue(all("reply_markup" not in g for g in got[:-1]))
        self.assertEqual(got[-1]["reply_markup"], mk)

        svc2, got2, _ = self._server(reject_markup=True)
        self.assertTrue(svc2.send("bad < markup <b>x</b> here <q>", reply_markup=mk).ok)
        self.assertEqual(len(got2), 2)
        self.assertEqual(got2[1]["reply_markup"], mk)                                    # buttons kept on the plain-text resend

    def test_total_failure_falls_back_to_a_file_with_the_full_text(self):
        svc, got, d = self._server(always_fail=True)
        r = svc.send("something important")
        self.assertFalse(r.ok)
        self.assertTrue(r.fell_back_to_file)
        self.assertIn("something important", next(d.glob("*.txt")).read_text(encoding="utf-8"))

    def test_disabled_and_no_token(self):
        svc, got, _ = self._server()
        svc.enabled = False
        self.assertEqual(svc.send("x").error, "disabled")
        svc.enabled, svc.bot_token = True, ""
        self.assertEqual(svc.send("x").error, "no_token")
        self.assertEqual(got, [])

    def test_split_message_helper(self):
        cls = _load_service()
        self.assertEqual(cls.split_message("short"), ["short"])
        self.assertEqual(cls.strip_html("<b>a &amp; b</b> <code>c</code>"), "a & b c")


class TradesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rt = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.now = time.mktime((2026, 9, 20, 12, 0, 0, 0, 0, -1))

    def _w(self, name, obj):
        (self.rt / name).write_text(json.dumps(obj), encoding="utf-8")

    def test_parses(self):
        for phrase in ("trades", "/trades", "paper trading", "Paper trades?", "trading"):
            self.assertEqual(commands.parse(phrase)[0], "trades", phrase)
        self.assertIsNone(commands.parse("trades are fun"))

    def test_no_files_says_so(self):
        self.assertIn("No paper-trading records", qol.trades_text(self.rt, self.now))

    def test_reads_legacy_journal_and_state_and_flags_quiet(self):
        self._w("trade_journal.json", {"trades": [
            {"symbol": "GME", "time": "2026-05-04T13:31:42.350342", "closed": False, "pnl": None},
            {"symbol": "AAA", "time": "2026-05-03T10:00:00", "closed": True, "pnl": 12.5},
            {"symbol": "BBB", "time": "2026-05-03T11:00:00", "closed": True, "pnl": -3.0}], "learnings": [], "stats": {}})
        self._w("trade_state.json", {"daily_pnl": -28.66, "daily_pnl_date": "2026-05-04", "portfolio_open": 100087.98})
        t = qol.trades_text(self.rt, self.now)
        self.assertIn("Trades on record: <b>3</b>", t)
        self.assertIn("2 with a recorded result, 1 winning", t)
        self.assertIn("paper equity $100,087.98", t)
        self.assertIn("-$28.66 on 2026-05-04", t)
        self.assertIn("2026-05-04 (138 days ago)", t)
        self.assertIn("quiet", t)
        self.assertIn("Real-money trading stays off", t)

    def test_recent_activity_is_not_flagged_and_history_file_wins(self):
        self._w("trade_history.json", [{"symbol": "X", "time": "2026-09-19T15:00:00", "pnl_pct": 1.5}])
        self._w("trade_journal.json", {"trades": [{"symbol": "OLD", "time": "2026-05-04T13:31:42"}]})
        t = qol.trades_text(self.rt, self.now)
        self.assertIn("Trades on record: <b>1</b>", t)
        self.assertNotIn("quiet", t)

    def test_kill_switch_and_garbage_are_handled(self):
        self._w("trade_state.json", {"killed_today": True, "daily_pnl": "n/a", "portfolio_open": None})
        (self.rt / "trade_history.json").write_text("{not json", encoding="utf-8")
        t = qol.trades_text(self.rt, self.now)
        self.assertIn("kill switch tripped", t)

    def _report(self, **over):
        rep = json.loads('''{"updated": "2026-09-21T14:15:00", "mode": "paper simulation (no orders are ever placed)", "data_source": "yahoo",
                  "bankroll_start": 100000.0, "equity": 102300.0, "return_pct": 2.3, "spy_return_pct": 2.0, "excess_pct": 0.3,
                  "max_drawdown_pct": -1.1, "days_tracked": 5, "inception": "2026-09-15", "trades_closed": 4, "wins": 3, "losses": 1,
                  "win_rate_pct": 75.0, "realized_pnl": 900.0, "killed": false, "dead": false, "died_at": null, "lifeforce_pct": 102.3,
                  "peak_equity": 103000.0, "drawdown_from_peak_pct": 0.68, "dies_at_drawdown_pct": 35, "last_scan": "2026-09-21T14:15:00",
                  "open_positions": [{"symbol": "AAPL", "qty": 10, "entry": 100.0, "core": false, "pnl_pct": 1.5},
                                     {"symbol": "SPY", "qty": 200, "entry": 500.0, "core": true, "pnl_pct": null}]}''')
        rep.update(over)
        self._w("trade_report.json", rep)
        return rep

    def test_report_shows_lifeforce_excess_over_spy_and_holdings(self):
        self._report()
        t = qol.trades_text(self.rt, self.now)
        self.assertIn("$102,300.00", t)
        self.assertIn("102.3% of its $100,000.00 start", t)
        self.assertIn("dies at 35%", t)
        self.assertIn("excess +0.30%", t)
        self.assertIn("3 won, 1 lost, 75% win rate", t)
        self.assertIn("AAPL +1.50%", t)
        self.assertIn("+ 200 SPY core", t)
        self.assertIn("Real-money trading stays off", t)
        self.assertNotIn("DEAD", t)

    def test_dead_agent_is_loud(self):
        self._report(dead=True, died_at="2026-09-22T10:00:00", lifeforce_pct=61.0, equity=61000.0)
        t = qol.trades_text(self.rt, self.now)
        self.assertIn("DEAD", t)
        self.assertIn("2026-09-22T10:00:00", t)
        self.assertIn("DEAD", qol.trades_digest_line(self.rt))

    def test_before_the_first_scan_it_says_when_that_will_be(self):
        self._report(last_scan="", spy_return_pct=None, excess_pct=None, days_tracked=0, inception=None, trades_closed=0, open_positions=[])
        t = qol.trades_text(self.rt, self.now)
        self.assertIn("No scan yet", t)
        self.assertIn("08:00 Denver", t)
        self.assertIn("comparison starts", t)
        self.assertEqual(qol.trades_digest_line(self.rt), "")                            # nothing to nag about yet

    def test_a_stale_report_asks_if_it_is_still_scheduled(self):
        self._report(last_scan="2026-09-01T14:15:00")
        self.assertIn("still scheduled", qol.trades_text(self.rt, self.now))

    def test_digest_line(self):
        self._report()
        line = qol.trades_digest_line(self.rt)
        self.assertIn("lifeforce 102.3%", line)
        self.assertIn("vs SPY +2.00%", line)
        self.assertIn("1 picks + core", line)
        self._report(killed=True)
        self.assertIn("kill switch tripped", qol.trades_digest_line(self.rt))
        self.assertEqual(qol.trades_digest_line(self.rt / "nowhere"), "")

    def test_a_report_without_lifeforce_falls_back_to_the_legacy_view(self):
        self._w("trade_report.json", {"equity": 1})
        self._w("trade_state.json", {"daily_pnl": 1.0, "daily_pnl_date": "2026-09-20", "portfolio_open": 5.0})
        self.assertIn("Last state", qol.trades_text(self.rt, self.now))

    def test_report_text_from_the_file_is_escaped(self):
        self._report(data_source="<script>x</script>", inception="<b>y</b>")
        t = qol.trades_text(self.rt, self.now)
        self.assertNotIn("<script>", t)
        self.assertNotIn("<b>y</b>", t)

    def test_model_or_file_text_is_escaped(self):
        self._w("trade_state.json", {"daily_pnl": 1.0, "daily_pnl_date": "<b>x</b>"})
        self.assertNotIn("<b>x</b>", qol.trades_text(self.rt, self.now))


if __name__ == "__main__":
    unittest.main()
