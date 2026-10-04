"""Fabrication lane (describe a part -> build123d in the sandbox -> card -> print on a tap) and the printer client."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.foundation import fabricate, printer  # noqa: E402

_spec = importlib.util.spec_from_file_location("cad_build", ROOT / "mission_sandbox" / "tools" / "cad_build.py")
cad_build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cad_build)

GOOD_CODE = "from build123d import *\nwith BuildPart() as p:\n    Box(40, 20, 5)\nresult = p.part\n"
REPORT = {"tool": "cad_build", "watertight": True, "volume_cm3": 4.0, "area_cm2": 22.0, "extents_mm": [40.0, 20.0, 5.0],
          "fits_bed": True, "bed_mm": [220, 220, 250], "warnings": []}
COST_MD = ("| Option | Est. low | Est. high | Fits | Notes |\n|---|---|---|---|---|\n"
           "| FDM PLA, print at home (material only) | $0.10 | $0.10 | yes | ~5 g of filament, ~0.2 h |\n")


def draft(code=GOOD_CODE, title="Test plate"):
    return json.dumps({"title": title, "explanation": "A 40x20x5 mm plate, printed flat.", "code": code})


class CadBuildPureTests(unittest.TestCase):
    def test_bed_parsing_and_fit(self):
        self.assertEqual(cad_build.parse_bed("220x220x250"), (220.0, 220.0, 250.0))
        for bad in ("220x220", "0x10x10", "a x b x c", "3000x10x10"):
            with self.assertRaises(ValueError):
                cad_build.parse_bed(bad)
        self.assertTrue(cad_build.fits_bed([250, 10, 10], (220, 220, 250)))          # fits lying on its side
        self.assertFalse(cad_build.fits_bed([300, 10, 10], (220, 220, 250)))

    def test_report_matches_ct_to_stl_keys_and_warns(self):
        r = cad_build.build_report([40, 20, 0.4], 320.0, 2200.0, False, (220, 220, 250))
        for k in ("watertight", "volume_cm3", "area_cm2", "extents_mm"):            # what print_cost.py reads
            self.assertIn(k, r)
        self.assertIsNone(r["volume_cm3"])                                          # no volume for a leaky mesh
        self.assertEqual(len(r["warnings"]), 2)                                     # not watertight + too thin

    def test_script_must_assign_result(self):
        with self.assertRaises(ValueError):
            cad_build.load_result("x = 1\n")
        self.assertEqual(cad_build.load_result("result = 42\n"), 42)


class CheckScriptTests(unittest.TestCase):
    def test_accepts_cad_and_rejects_everything_else(self):
        self.assertEqual(fabricate.check_script(GOOD_CODE), "")
        self.assertIn("imports os", fabricate.check_script("import os\nresult = 1\n"))
        self.assertIn("imports urllib", fabricate.check_script("from urllib import request\nresult = 1\n"))
        self.assertIn("result", fabricate.check_script("from build123d import *\nx = 1\n"))
        self.assertIn("does not parse", fabricate.check_script("result = (\n"))


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = {"AURIX_PROJECT_ROOT": self.tmp.name, "ANTHROPIC_API_KEY": "", "AURIX_PRINTER_KIND": "", "AURIX_PRINTER_URL": ""}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        self.runs = []

    def runner(self, fail_first=False, missing_layer=False):
        def run(ws_id, tool, code, timeout):
            self.runs.append((ws_id, code))
            ws = Path(self.tmp.name) / "data" / "workspace" / ws_id
            if "cad_build.py" in code:
                if missing_layer:
                    return {"output": "STDERR: ERROR: the CAD layer is not installed in this sandbox image", "exit_code": 2}
                if fail_first and len(self.runs) == 1:
                    return {"output": "STDERR: ERROR: NameError: name 'Bx' is not defined", "exit_code": 1}
                (ws / "part.stl").write_bytes(b"solid x\nendsolid x\n")
                (ws / "report.json").write_text(json.dumps(REPORT))
                (ws / "cost.md").write_text(COST_MD)
                return {"output": json.dumps(REPORT), "exit_code": 0}
            if "prusa-slicer" in code:
                (ws / "part.gcode").write_text("G28\n")
                return {"output": "", "exit_code": 0}
            return {"output": "?", "exit_code": 1}
        return run


class RequestTests(_Base):
    def test_draft_build_price_and_card(self):
        local = mock.Mock(return_value=(draft(), ""))
        msg = fabricate.request("a 40 by 20 mm test plate, 5 mm thick", local=local, run=self.runner())
        self.assertIn("📐 <b>Test plate</b>", msg)
        self.assertIn("40 × 20 × 5 mm · 4 cm³ · watertight", msg)
        self.assertIn("$0.10 of filament at home", msg)
        job = fabricate.pending()[0]
        self.assertRegex(job["id"], r"^d-[0-9a-f]{6}$")
        self.assertRegex(job["workspace"], r"^m-[0-9a-f]{6}$")                      # the sandbox only accepts m- workspaces
        self.assertEqual(job["attempts"], 1)

    def test_a_failed_build_gets_one_repair_with_the_error(self):
        local = mock.Mock(side_effect=[(draft("from build123d import *\nresult = Bx(1, 1, 1)\n"), ""), (draft(), "")])
        msg = fabricate.request("a 40 by 20 mm test plate, 5 mm thick", local=local, run=self.runner(fail_first=True))
        self.assertIn("Test plate", msg)
        self.assertIn("NameError", local.call_args_list[1][0][1])                   # the repair prompt carries the real error
        self.assertEqual(fabricate.pending()[0]["attempts"], 2)

    def test_missing_cad_layer_is_reported_not_retried(self):
        local = mock.Mock(return_value=(draft(), ""))
        msg = fabricate.request("a 40 by 20 mm test plate, 5 mm thick", local=local, run=self.runner(missing_layer=True))
        self.assertIn("docker compose build sandbox", msg)
        self.assertEqual(local.call_count, 1)
        self.assertEqual(fabricate.pending(), [])

    def test_non_cad_code_never_reaches_the_sandbox(self):
        local = mock.Mock(return_value=(draft("import os\nos.system('x')\nresult = 1\n"), ""))
        msg = fabricate.request("a 40 by 20 mm test plate, 5 mm thick", local=local, run=self.runner())
        self.assertIn("imports os", msg)
        self.assertEqual(self.runs, [])

    def test_too_short(self):
        self.assertIn("Describe the part", fabricate.request("clip", run=self.runner()))


class FakePrinter:
    """Moonraker/OctoPrint/PrusaLink stand-in: records requests, answers status from `state`."""

    def __init__(self, state="standby"):
        self.state, self.calls = state, []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if url.endswith("/printer/objects/query?print_stats&display_status"):
            return 200, json.dumps({"result": {"status": {"print_stats": {"state": self.state, "filename": "a.gcode"},
                                                          "display_status": {"progress": 0.42}}}}).encode()
        if url.endswith("/api/job"):
            return 200, json.dumps({"state": "Operational" if self.state == "standby" else "Printing",
                                    "progress": {"completion": 12.5}, "job": {"file": {"name": "b.gcode"}}}).encode()
        if url.endswith("/api/v1/status"):
            return 200, json.dumps({"printer": {"state": "IDLE" if self.state == "standby" else "PRINTING"},
                                    "job": {"progress": 77}}).encode()
        return 201, b"{}"


class ApproveTests(_Base):
    def _job(self):
        fabricate.request("a 40 by 20 mm test plate, 5 mm thick", local=mock.Mock(return_value=(draft(), "")), run=self.runner())
        return fabricate.pending()[0]

    def test_without_a_printer_the_stl_is_kept(self):
        job = self._job()
        msg = fabricate.approve(job["id"], run=self.runner())
        self.assertIn("Kept", msg)
        self.assertIn("part.stl", msg)
        self.assertEqual(fabricate.get(job["id"])["status"], "kept")

    def test_prints_only_when_idle_after_slicing_with_the_owners_profile(self):
        job = self._job()
        fabricate.profile_path().parent.mkdir(parents=True, exist_ok=True)
        fabricate.profile_path().write_text("[printer]\n")
        with mock.patch.dict(os.environ, {"AURIX_PRINTER_KIND": "moonraker", "AURIX_PRINTER_URL": "http://printer.local:7125"}):
            busy = FakePrinter(state="printing")
            msg = fabricate.approve(job["id"], run=self.runner(), http=busy)
            self.assertIn("Not printing: printer is printing", msg)
            self.assertFalse(any(m == "POST" for m, *_ in busy.calls))               # never uploaded while busy
            self.assertEqual(fabricate.get(job["id"])["status"], "review")           # still waiting: tap again later
            idle = FakePrinter()
            msg = fabricate.approve(job["id"], run=self.runner(), http=idle)
        self.assertIn("started printing", msg)
        self.assertIn("prusa-slicer --export-gcode --load printer.ini", self.runs[-1][1])
        self.assertEqual(fabricate.get(job["id"])["status"], "printing")

    def test_decline_and_unknown_ids(self):
        job = self._job()
        self.assertIn("Discarded", fabricate.decline(job["id"]))
        self.assertIn("No part waiting", fabricate.approve(job["id"]))
        self.assertIn("No part waiting", fabricate.decline("d-000000"))


class PrinterClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.gcode = Path(self.tmp.name) / "part.gcode"
        self.gcode.write_text("G28\n")

    def env(self, kind, url="http://printer.local", key="k123"):
        return mock.patch.dict(os.environ, {"AURIX_PRINTER_KIND": kind, "AURIX_PRINTER_URL": url, "AURIX_PRINTER_API_KEY": key})

    def test_status_per_api(self):
        for kind, expect in (("moonraker", ("idle", 42.0)), ("octoprint", ("idle", 12.5)), ("prusalink", ("idle", 77.0))):
            with self.subTest(kind=kind), self.env(kind):
                st = printer.status(FakePrinter())
                self.assertEqual((st["state"], st["progress"]), expect)

    def test_upload_shapes(self):
        with self.env("moonraker", key=""):
            fp = FakePrinter()
            ok, _ = printer.send_and_print(str(self.gcode), fp)
            method, url, headers, body = fp.calls[-1]
            self.assertTrue(ok)
            self.assertEqual((method, url), ("POST", "http://printer.local/server/files/upload"))
            self.assertIn(b'name="print"\r\n\r\ntrue', body)
            self.assertNotIn("X-Api-Key", headers)
        with self.env("octoprint"):
            fp = FakePrinter()
            printer.send_and_print(str(self.gcode), fp)
            self.assertEqual(fp.calls[-1][1], "http://printer.local/api/files/local")
            self.assertEqual(fp.calls[-1][2]["X-Api-Key"], "k123")
        with self.env("prusalink"):
            fp = FakePrinter()
            printer.send_and_print(str(self.gcode), fp)
            method, url, headers, body = fp.calls[-1]
            self.assertEqual((method, url, headers["Print-After-Upload"], body), ("PUT", "http://printer.local/api/v1/files/usb/part.gcode", "?1", b"G28\n"))

    def test_refusals(self):
        with self.env("octoprint"):
            self.assertFalse(printer.send_and_print(str(self.gcode), FakePrinter(state="printing"))[0])
            stl = Path(self.tmp.name) / "part.stl"
            stl.write_text("solid")
            self.assertEqual(printer.send_and_print(str(stl), FakePrinter()), (False, "not a G-code file"))
        with self.env("bambu"):
            self.assertFalse(printer.configured())
            self.assertEqual(printer.status()["state"], "unconfigured")

    def test_offline_printer_reads_as_offline(self):
        def down(*a):
            raise OSError("no route")
        with self.env("moonraker"):
            self.assertEqual(printer.status(down)["state"], "offline")


class WiringTests(_Base):
    def test_commands_and_cards(self):
        from src.foundation import actions, commands
        self.assertEqual(commands.parse("cad: a 40 mm cable clip, 4 mm hole"), ("fab", "a 40 mm cable clip, 4 mm hole"))
        self.assertEqual(commands.parse("Print D-1A2B3C"), ("fab_yes", "d-1a2b3c"))
        self.assertEqual(commands.parse("printer"), ("printer", ""))
        fabricate.request("a 40 by 20 mm test plate, 5 mm thick", local=mock.Mock(return_value=(draft(), "")), run=self.runner())
        job = fabricate.pending()[0]
        cards = [c for c in actions.decisions() if c["kind"] == "fabricate"]
        self.assertEqual([b["action"] for b in cards[0]["buttons"]], ["fab_yes", "fab_no"])
        self.assertEqual(cards[0]["buttons"][0]["label"], "Keep STL")                # no printer set up: says so
        res = actions.run_action("fab_no", job["id"])
        self.assertTrue(res["ok"], res)
        self.assertEqual(fabricate.get(job["id"])["status"], "discarded")


if __name__ == "__main__":
    unittest.main()
