#!/usr/bin/env python3
"""Build Zero Hour: Sundown's assets from Rascal's own asset packs.

    python3 games/sundown/tools/build_assets.py --pack Frontier1864.zip --pack FrontierSaloon.zip \
        --pack Rainwire2089.zip

Each --pack is a pack zip or an unzipped pack folder (anything holding */Sprites/96px/*.json).
Output goes to static/games/sundown/assets/ (git-ignored: the packs are paid assets and the Aurix
repo is public, so the built sprites are never committed).

Writes:
  assets/sprites/<Name>.png     sprite sheets (rows = 8 directions, cols = animation frames)
  assets/tiles.png              procedural ground tiles (original, drawn here)
  assets/font1.png font2.png    Rainwire Mono bitmap font
  assets/manifest.js            window.SD_ASSETS = {...}  (a .js file so file:// play works)
"""
from __future__ import annotations

import argparse
import io
import json
import random
import re
import sys
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
from recolor import VARIANTS, recolor  # noqa: E402

HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE.parents[2] / "static" / "games" / "sundown" / "assets"

# Sheets the game uses, by pack sprite name.
WANTED = [
    # Frontier 1864
    "Barrel", "Boardwalk", "Campfire", "CoveredWagon", "Crate", "DynamiteCrate", "HayBales",
    "HitchingPost", "MineCart", "MineEntrance", "MinerTent", "OrePile", "Prospector",
    "ProspectorIdle", "RainBarrel", "RockSet", "SaloonDoors", "SluiceBox", "SplitRailFence",
    "TelegraphPole", "WagonWheel", "WantedBoard", "WaterTrough",
    # Saloon Interior
    "BackBar", "BarCounter", "BarStool", "Bartender", "BartenderIdle", "BartenderWipe", "CardTable",
    "Chair", "FloorSection", "HangingLamp", "KegStand", "OverturnedChair", "Piano", "SquareTable",
    "WallSconce", "WallSection", "WhiskeyCrate",
    # Rainwire 2089
    "ACUnits", "Barrier", "BladeSign", "CablePole", "CargoCrate", "Dumpster", "HoloBillboard",
    "HoverDrone", "JunctionBox", "LEDCone", "NeonBarSign", "NoodleStall", "SatelliteDish",
    "SecurityCam", "ServerRack", "SteamVent", "StreetKiosk", "StreetLamp", "TrashPile",
    "VendingMachine",
]
# Target size in world pixels for each sheet's largest extent. The packs fit every model to its
# own 96 px frame, so a barrel and a wagon arrive the same size; these put them back in scale
# (a person is ~52 px tall, about 29 px per metre).
TARGET = {
    "Barrel": 34, "Boardwalk": 90, "Campfire": 40, "CoveredWagon": 150, "Crate": 36, "DynamiteCrate": 44,
    "HayBales": 46, "HitchingPost": 56, "MineCart": 60, "MineEntrance": 140, "MinerTent": 96, "OrePile": 50,
    "RainBarrel": 46, "RockSet": 60, "SaloonDoors": 56, "SluiceBox": 80, "SplitRailFence": 80,
    "TelegraphPole": 110, "WagonWheel": 34, "WantedBoard": 64, "WaterTrough": 64,
    "BackBar": 90, "BarCounter": 90, "BarStool": 40, "CardTable": 48, "Chair": 40, "FloorSection": 96,
    "HangingLamp": 40, "KegStand": 50, "OverturnedChair": 44, "Piano": 64, "SquareTable": 48,
    "WallSconce": 30, "WallSection": 96, "WhiskeyCrate": 38,
    "ACUnits": 56, "Barrier": 60, "BladeSign": 70, "CablePole": 110, "CargoCrate": 50, "Dumpster": 70,
    "HoloBillboard": 96, "HoverDrone": 44, "JunctionBox": 50, "LEDCone": 30, "NeonBarSign": 56,
    "NoodleStall": 90, "SatelliteDish": 56, "SecurityCam": 70, "ServerRack": 64, "SteamVent": 56,
    "StreetKiosk": 70, "StreetLamp": 100, "TrashPile": 48, "VendingMachine": 64,
}
PERSON_H = 52            # characters: height in world px
BIG = {"Reynolds": 74}   # boss variants drawn bigger
FLASH = {"Prospector", "Outlaw", "Rustler", "Bandit", "Reynolds", "Glitch", "HoverDrone", "Bartender"}

FONT_FILES = ["rainwire_mono_1x.fnt", "rainwire_mono_1x.png", "rainwire_mono_2x.fnt", "rainwire_mono_2x.png"]


class PackSource:
    """Reads sprite json/png and font files out of zips or folders."""

    def __init__(self, paths: list[Path]):
        self.files: dict[str, tuple[Path, str | None]] = {}  # basename -> (container, member)
        for p in paths:
            if p.is_file() and p.suffix == ".zip":
                with zipfile.ZipFile(p) as z:
                    for m in z.namelist():
                        self._add(m, p, m)
            elif p.is_dir():
                for f in p.rglob("*"):
                    if f.is_file():
                        self._add(str(f), f, None)
            else:
                raise SystemExit(f"--pack {p}: not a zip or folder")

    def _add(self, name: str, container: Path, member: str | None):
        norm = name.replace("\\", "/")
        if "/Sprites/96px/" in norm or "/UI/font/" in norm:
            self.files.setdefault(norm.rsplit("/", 1)[-1], (container, member))

    def read(self, base: str) -> bytes:
        if base not in self.files:
            raise KeyError(base)
        container, member = self.files[base]
        if member is None:
            return container.read_bytes()
        with zipfile.ZipFile(container) as z:
            return z.read(member)

    def has(self, base: str) -> bool:
        return base in self.files


def frame_bbox(sheet: Image.Image, fw: int, fh: int, row: int = 0, col: int = 0):
    fr = sheet.crop((col * fw, row * fh, (col + 1) * fw, (row + 1) * fh))
    return fr.getchannel("A").point(lambda a: 255 if a > 40 else 0).getbbox()


def scale_sheet(im: Image.Image, meta: dict, scale: float) -> tuple[Image.Image, dict]:
    """Resample a whole sheet so each frame is round(96*scale) px, keeping hard pixel edges."""
    fw = max(8, round(meta["frame_w"] * scale))
    fh = max(8, round(meta["frame_h"] * scale))
    out = im.resize((fw * meta["cols"], fh * meta["rows"]), Image.NEAREST if scale >= 1 else Image.LANCZOS)
    a = out.getchannel("A").point(lambda v: 255 if v > 100 else 0)
    out.putalpha(a)
    return out, {**meta, "frame_w": fw, "frame_h": fh}


def white_of(im: Image.Image) -> Image.Image:
    w = Image.new("RGBA", im.size, (255, 255, 255, 0))
    w.putalpha(im.getchannel("A"))
    return w


def sheet_meta(sheet: Image.Image, meta: dict) -> dict:
    fw, fh, rows, cols = meta["frame_w"], meta["frame_h"], meta["rows"], meta["cols"]
    # union bbox over all directions of frame 0: gives a stable foot anchor
    boxes = [b for r in range(rows) if (b := frame_bbox(sheet, fw, fh, r, 0))]
    if not boxes:
        boxes = [(0, 0, fw, fh)]
    x0 = min(b[0] for b in boxes); y0 = min(b[1] for b in boxes)
    x1 = max(b[2] for b in boxes); y1 = max(b[3] for b in boxes)
    front = frame_bbox(sheet, fw, fh, 0, 0) or (x0, y0, x1, y1)
    return {
        "fw": fw, "fh": fh, "rows": rows, "cols": cols,
        "ax": round((front[0] + front[2]) / 2), "ay": y1,   # feet anchor inside the frame
        "bw": x1 - x0, "bh": y1 - y0, "top": y0,
    }


def parse_fnt(text: str) -> dict:
    glyphs = {}
    line_h = 11
    for line in text.splitlines():
        kv = dict(re.findall(r"(\w+)=(-?\d+)", line))
        if line.startswith("common"):
            line_h = int(kv["lineHeight"])
        elif line.startswith("char "):
            glyphs[int(kv["id"])] = [int(kv[k]) for k in ("x", "y", "width", "height", "xoffset", "yoffset", "xadvance")]
    return {"lineHeight": line_h, "glyphs": glyphs}


# --------------------------------------------------------------------------- ground tiles
T = 32
TILE_NAMES = ["dirt0", "dirt1", "dirt2", "dirt3", "pebbles", "grass0", "grass1", "rut_h", "rut_v",
              "sand", "rift0", "rift1", "rift_grid", "plank", "crack", "scorch"]


def _noise_tile(rng, base, var, speck=None, n_speck=0):
    im = Image.new("RGBA", (T, T), base)
    px = im.load()
    for y in range(T):
        for x in range(T):
            d = rng.randint(-var, var)
            r, g, b, _ = base
            px[x, y] = (max(0, min(255, r + d)), max(0, min(255, g + d)), max(0, min(255, b + d)), 255)
    if speck:
        for _ in range(n_speck):
            x, y = rng.randrange(T), rng.randrange(T)
            c = speck[rng.randrange(len(speck))]
            px[x, y] = c
            if rng.random() < 0.4 and x + 1 < T:
                px[x + 1, y] = c
    return im


def build_tiles(out: Path, seed: int = 1864) -> dict:
    rng = random.Random(seed)
    dirt = (150, 112, 74, 255)
    tiles = []
    for i in range(4):
        tiles.append(_noise_tile(rng, (dirt[0] + i * 3 - 4, dirt[1] + i * 2 - 3, dirt[2] + i - 2, 255), 6,
                                 [(120, 88, 58, 255), (172, 134, 92, 255), (110, 80, 52, 255)], 26))
    pb = _noise_tile(rng, dirt, 6, [(186, 176, 160, 255), (124, 116, 106, 255), (96, 90, 84, 255)], 40)
    tiles.append(pb)
    for i in range(2):
        g = _noise_tile(rng, dirt, 6, [(120, 88, 58, 255)], 20)
        d = ImageDraw.Draw(g)
        for _ in range(7 + i * 4):
            x, y = rng.randrange(2, T - 2), rng.randrange(4, T)
            col = rng.choice([(104, 122, 58, 255), (126, 140, 66, 255), (88, 104, 48, 255)])
            for k in range(rng.randint(2, 4)):
                d.line([(x, y), (x + rng.randint(-2, 2), y - rng.randint(2, 5))], fill=col)
        tiles.append(g)
    for horiz in (True, False):
        r = _noise_tile(rng, dirt, 5, [(120, 88, 58, 255)], 16)
        d = ImageDraw.Draw(r)
        for off in (9, 22):
            for t in range(T):
                if rng.random() < 0.85:
                    p = (t, off + rng.randint(0, 1)) if horiz else (off + rng.randint(0, 1), t)
                    d.point(p, fill=(112, 82, 54, 255))
                    d.point((p[0] + (0 if horiz else 1), p[1] + (1 if horiz else 0)), fill=(128, 94, 62, 255))
        tiles.append(r)
    tiles.append(_noise_tile(rng, (176, 146, 102, 255), 5, [(196, 168, 124, 255)], 30))
    # rift ground: dark asphalt with neon specks and a grid tile
    for i in range(2):
        tiles.append(_noise_tile(rng, (22, 20, 34, 255), 4, [(0, 240, 255, 255), (255, 46, 196, 255), (40, 36, 60, 255)], 6 + i * 4))
    grid = _noise_tile(rng, (18, 16, 30, 255), 3)
    d = ImageDraw.Draw(grid)
    d.line([(0, 0), (T - 1, 0)], fill=(0, 200, 230, 255))
    d.line([(0, 0), (0, T - 1)], fill=(0, 200, 230, 255))
    d.point((T // 2, T // 2), fill=(255, 46, 196, 255))
    tiles.append(grid)
    plank = Image.new("RGBA", (T, T), (122, 86, 52, 255))
    d = ImageDraw.Draw(plank)
    for y in range(0, T, 8):
        d.line([(0, y), (T - 1, y)], fill=(82, 56, 34, 255))
        for x in range(T):
            if rng.random() < 0.25:
                d.point((x, y + rng.randint(2, 6)), fill=(108, 74, 44, 255))
        nx = rng.randrange(T)
        d.line([(nx, y + 1), (nx, y + 7)], fill=(92, 62, 38, 255))
    tiles.append(plank)
    cr = _noise_tile(rng, dirt, 6, [(120, 88, 58, 255)], 16)
    d = ImageDraw.Draw(cr)
    x, y = 4, 6
    for _ in range(14):
        nx, ny = x + rng.randint(1, 3), y + rng.randint(0, 2)
        d.line([(x, y), (nx, ny)], fill=(84, 60, 40, 255))
        x, y = nx, ny
    tiles.append(cr)
    sc = _noise_tile(rng, dirt, 6)
    px = sc.load()
    for y in range(T):
        for x in range(T):
            dd = ((x - 16) ** 2 + (y - 16) ** 2) ** 0.5
            if dd < 14 + rng.random() * 2:
                k = 0.35 + 0.5 * dd / 16
                r, g, b, a = px[x, y]
                px[x, y] = (int(r * k), int(g * k), int(b * k), a)
    tiles.append(sc)
    atlas = Image.new("RGBA", (T * len(tiles), T))
    for i, t in enumerate(tiles):
        atlas.alpha_composite(t, (i * T, 0))
    atlas.save(out / "tiles.png")
    return {"src": "assets/tiles.png", "size": T, "names": TILE_NAMES[: len(tiles)]}


def build(packs: list[Path], out: Path) -> dict:
    src = PackSource(packs)
    (out / "sprites").mkdir(parents=True, exist_ok=True)
    sheets, missing = {}, []
    for name in WANTED:
        if not src.has(f"{name}.json"):
            missing.append(name)
            continue
        meta = json.loads(src.read(f"{name}.json"))
        raw = Image.open(io.BytesIO(src.read(meta["image"]))).convert("RGBA")
        people = name.startswith(("Prospector", "Bartender"))

        def emit(key: str, img: Image.Image, target: float | None):
            if target is None:   # person
                bb = sheet_meta(img, meta)
                sc = PERSON_H / max(1, bb["bh"])
            else:
                bb = sheet_meta(img, meta)
                sc = target / max(1, bb["bw"], bb["bh"])
            small, m2 = scale_sheet(img, meta, sc)
            small.save(out / "sprites" / f"{key}.png", optimize=True)
            sheets[key] = {"src": f"assets/sprites/{key}.png", "scale": round(sc, 4), **sheet_meta(small, m2)}
            if key.rstrip("Idle") in FLASH or key in FLASH:
                white_of(small).save(out / "sprites" / f"{key}_w.png", optimize=True)
                sheets[key]["white"] = f"assets/sprites/{key}_w.png"

        emit(name, raw, None if people else TARGET.get(name, 60))
        if name in ("Prospector", "ProspectorIdle"):
            for vname, spec in VARIANTS.items():
                v = recolor(raw, spec, glitch=(vname == "Glitch"))
                key = vname + ("Idle" if name.endswith("Idle") else "")
                if vname in BIG:
                    bb = sheet_meta(v, meta)
                    small, m2 = scale_sheet(v, meta, BIG[vname] / bb["bh"])
                    small.save(out / "sprites" / f"{key}.png", optimize=True)
                    sheets[key] = {"src": f"assets/sprites/{key}.png", **sheet_meta(small, m2)}
                    white_of(small).save(out / "sprites" / f"{key}_w.png", optimize=True)
                    sheets[key]["white"] = f"assets/sprites/{key}_w.png"
                else:
                    emit(key, v, None)
    if missing:
        print("missing sprites (game falls back to boxes):", ", ".join(missing), file=sys.stderr)
    font = {}
    for scale in (1, 2):
        fnt, png = f"rainwire_mono_{scale}x.fnt", f"rainwire_mono_{scale}x.png"
        if src.has(fnt) and src.has(png):
            (out / f"font{scale}.png").write_bytes(src.read(png))
            font[str(scale)] = {"src": f"assets/font{scale}.png", **parse_fnt(src.read(fnt).decode("utf-8", "replace"))}
    manifest = {"version": 1, "sheets": sheets, "tiles": build_tiles(out), "font": font, "missing": missing}
    (out / "manifest.js").write_text("window.SD_ASSETS=" + json.dumps(manifest, separators=(",", ":")) + ";\n")
    return manifest


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pack", action="append", type=Path, required=True, help="pack zip or folder (repeatable)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    m = build(a.pack, a.out)
    print(f"built {len(m['sheets'])} sheets, {len(m['tiles']['names'])} tiles, font {list(m['font'])} -> {a.out}")


if __name__ == "__main__":
    main()
