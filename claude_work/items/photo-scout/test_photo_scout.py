"""Tests for photo_scout.py. Run: python3 -m pytest -q test_photo_scout.py  (or python3 test_photo_scout.py)

Builds synthetic photos, runs the scout against a fake ollama, and checks that it
filters correctly and never touches the source folder.
"""
import csv
import hashlib
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

sys.path.insert(0, str(Path(__file__).parent))
import photo_scout as ps  # noqa: E402


def _scene(seed, w=2600, h=1800):
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    base = np.stack([128 + 90 * np.sin(x / (40 + seed * 7) + seed),
                     128 + 90 * np.cos(y / (55 + seed * 5)),
                     128 + 60 * np.sin((x + y) / (70 + seed))], -1)
    base += rng.normal(0, 18, base.shape)
    for _ in range(40):  # hard-edged shapes so it is genuinely sharp
        cx, cy, r = rng.integers(0, w), rng.integers(0, h), rng.integers(20, 160)
        base[max(0, cy - r):cy + r, max(0, cx - r):cx + r] = rng.integers(0, 255, 3)
    return Image.fromarray(np.clip(base, 0, 255).astype(np.uint8))


def make_library(d: Path):
    d.mkdir(parents=True, exist_ok=True)
    (d / "trip").mkdir()
    for i in range(4):
        ex = Image.Exif()
        ex[271], ex[272] = "Apple", "iPhone 13"
        if i == 1:
            ex[0x8825] = {1: "N", 2: (39.0, 44.0, 0.0)}
        _scene(i + 1).save(d / f"good_{i}.jpg", quality=92, exif=ex)
    _scene(1).resize((2500, 1731)).save(d / "trip" / "good_0_copy.jpg", quality=80)    # near-duplicate
    _scene(5).filter(ImageFilter.GaussianBlur(9)).save(d / "blurry.jpg", quality=92)
    _scene(6, 900, 600).save(d / "tiny.jpg")                                            # < 4 MP
    Image.fromarray((np.asarray(_scene(7)) * 0.05).astype(np.uint8)).save(d / "dark.jpg")
    (d / "broken.jpg").write_bytes(b"not a jpeg")
    (d / "raw.cr2").write_bytes(b"raw")
    (d / "notes.txt").write_text("ignore me")


def snapshot(d: Path):
    out = {}
    for p in sorted(d.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[str(p)] = (hashlib.sha256(p.read_bytes()).hexdigest(), st.st_size, st.st_mtime)
    return out


class FakeOllama(BaseHTTPRequestHandler):
    calls = 0

    def log_message(self, *a):
        pass

    def _send(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send({"models": [{"name": "qwen2.5vl:3b"}]})

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert req["images"] and req["format"] == "json"
        FakeOllama.calls += 1
        n = FakeOllama.calls
        reply = {"aesthetic": 8 - n % 3, "stock_appeal": 7, "title": f"Abstract colour field {n}",
                 "description": "Colourful abstract shapes.", "keywords": ["abstract", "colour", "Abstract", "pattern"],
                 "category": "graphic resources", "recognizable_people": n == 2, "private_property": False,
                 "logos_or_brands": "false", "license_plates": False, "artwork": False, "text_visible": False,
                 "notes": "test"}
        self._send({"response": "Sure! " + json.dumps(reply)})


def run_case(with_vision: bool):
    tmp = Path(tempfile.mkdtemp())
    src, out = tmp / "photos", tmp / "out"
    make_library(src)
    before = snapshot(src)
    args = ["--src", str(src), "--out", str(out), "--workers", "2"]
    srv = None
    if with_vision:
        FakeOllama.calls = 0
        srv = HTTPServer(("127.0.0.1", 0), FakeOllama)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        args += ["--ollama-url", f"http://127.0.0.1:{srv.server_port}", "--export"]
    else:
        args += ["--no-vision"]
    assert ps.main(args) == 0
    if srv:
        srv.shutdown()
    assert snapshot(src) == before, "source folder changed!"
    return src, out


def test_technical_only():
    src, out = run_case(False)
    rows = {Path(r["path"]).name: r for r in csv.DictReader(open(out / "all_scored.csv"))}
    assert rows["blurry.jpg"]["reject_reason"].startswith("blurry")
    assert rows["tiny.jpg"]["reject_reason"].startswith("too small")
    assert rows["dark.jpg"]["reject_reason"] in ("badly exposed", "heavily clipped")
    assert rows["broken.jpg"]["reject_reason"] == "unreadable"
    dups = list(csv.DictReader(open(out / "duplicates.csv")))
    assert len(dups) == 1 and {Path(dups[0]["duplicate"]).name, Path(dups[0]["kept"]).name} == {"good_0.jpg", "good_0_copy.jpg"}
    short = list(csv.DictReader(open(out / "shortlist.csv")))
    assert len(short) == 4 and all(r["action"].startswith("REVIEW") for r in short)
    assert (out / "contact_sheet.jpg").exists()
    assert rows["good_1.jpg"]["has_gps"] == "True" and rows["good_0.jpg"]["camera"] == "Apple iPhone 13"


def test_with_vision_and_rerun_uses_cache():
    src, out = run_case(True)
    short = list(csv.DictReader(open(out / "shortlist.csv")))
    assert len(short) == 4
    actions = [r["action"] for r in short]
    assert sum(a.startswith("SUBMIT") for a in actions) == 3
    assert sum(a.startswith("NEEDS RELEASE") for a in actions) == 1
    assert short[0]["keywords"] == "abstract, colour, pattern"           # deduped, lower-cased
    adobe = list(csv.DictReader(open(out / "adobe_stock_metadata.csv")))
    assert len(adobe) == 3 and adobe[0]["Category"] == "8"
    calls = FakeOllama.calls
    up = sorted(x.name for x in (out / "upload").iterdir())
    assert up == sorted(r["Filename"] for r in adobe) and len(up) == 3
    with Image.open(out / "upload" / up[0]) as im:
        assert im.format == "JPEG" and not im.getexif()
    srv = HTTPServer(("127.0.0.1", 0), FakeOllama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    assert ps.main(["--src", str(src), "--out", str(out), "--ollama-url", f"http://127.0.0.1:{srv.server_port}"]) == 0
    srv.shutdown()
    assert FakeOllama.calls == calls, "rerun should reuse cached vision results"


def test_vision_offline_degrades_gracefully():
    tmp = Path(tempfile.mkdtemp())
    make_library(tmp / "p")
    assert ps.main(["--src", str(tmp / "p"), "--out", str(tmp / "o"), "--ollama-url", "http://127.0.0.1:9"]) == 0
    short = list(csv.DictReader(open(tmp / "o" / "shortlist.csv")))
    assert short and all(r["action"].startswith("REVIEW") for r in short)


def test_refuses_out_equal_src():
    tmp = Path(tempfile.mkdtemp())
    try:
        ps.main(["--src", str(tmp), "--out", str(tmp)])
    except SystemExit as e:
        assert e.code != 0
    else:
        raise AssertionError("should refuse")


def test_refuses_out_inside_src_and_reads_two_folders():
    tmp = Path(tempfile.mkdtemp())
    make_library(tmp / "a")
    try:
        ps.main(["--src", str(tmp / "a"), "--out", str(tmp / "a" / "scout")])
    except SystemExit as e:
        assert e.code != 0
    else:
        raise AssertionError("should refuse --out inside --src")
    (tmp / "b").mkdir()
    _scene(9).save(tmp / "b" / "extra.jpg", quality=92)
    assert ps.main(["--src", str(tmp / "a"), str(tmp / "b"), "--out", str(tmp / "o"), "--no-vision"]) == 0
    rows = [Path(r["path"]).name for r in csv.DictReader(open(tmp / "o" / "all_scored.csv"))]
    assert "extra.jpg" in rows and "good_0.jpg" in rows


def test_parse_vision_robust():
    v = ps.parse_vision('```json\n{"aesthetic": "11", "stock_appeal": 0, "keywords": "a; b, a", "category": "nope"}\n```')
    assert v["aesthetic"] == 10 and v["stock_appeal"] == 1 and v["keywords"] == ["a", "b"] and v["category"] == ""


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
