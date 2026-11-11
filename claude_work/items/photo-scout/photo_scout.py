#!/usr/bin/env python3
"""AURIX photo scout: find the photos in a folder that are worth selling as stock.

READ-ONLY on your photos. It opens each file to look at it and never moves, renames,
edits, re-saves or deletes anything in the source folder. Everything it writes goes
to --out (which must not be the source folder).

Pipeline
  1. Scan --src recursively for images (jpg/jpeg/png/tif/tiff/webp, heic if pillow-heif is installed).
  2. Technical pass (CPU, parallel): resolution, sharpness (variance of Laplacian),
     exposure / clipping, contrast, colourfulness, EXIF (camera, date, GPS present).
  3. Drop: too small for stock, blurry, badly exposed; group near-duplicates (dHash)
     and keep only the best shot of each group.
  4. Vision pass (local ollama, optional) on the top --vision-top survivors:
     aesthetic + stock-appeal score, title, description, 25-45 keywords, category,
     and risk flags (recognisable people, private home/property, logos/brands,
     licence plates, artwork) that would need a release or make a shot editorial-only.
  5. Optional --export: sRGB JPEG copies of the submit-ready shots in --out/upload, EXIF/GPS
     stripped, with a light polish (auto-levels, gentle sharpen) unless --no-enhance.
  6. Write: shortlist.csv, all_scored.csv, duplicates.csv, adobe_stock_metadata.csv,
     contact_sheet.jpg, summary.txt.

Results are cached in --out/cache.json, so a rerun only re-reads new or changed files.

Usage
  python3 photo_scout.py --src ~/Pictures --out ~/ai/brain/photo_scout
  python3 photo_scout.py --src ~/Pictures --out /tmp/ps --no-vision      # fast, technical only
  python3 photo_scout.py --src ~/Pictures --out ~/ai/brain/photo_scout --export   # + upload-ready copies
  AURIX_PHOTO_DIR=~/Pictures python3 photo_scout.py

Needs: Python 3.9+, Pillow, numpy. Optional: pillow-heif (iPhone .HEIC), a running ollama
with a vision model (default qwen2.5vl:3b, ~3 GB, fits a 6 GB GTX 1660 Super).
"""
from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

try:
    from PIL import ImageCms
except Exception:  # pragma: no cover
    ImageCms = None

try:  # optional HEIC support (iPhone photos)
    from pillow_heif import register_heif_opener  # type: ignore

    register_heif_opener()
    HEIC_OK = True
except Exception:  # pragma: no cover - optional dependency
    HEIC_OK = False

Image.MAX_IMAGE_PIXELS = 200_000_000  # big panoramas are fine; still guards against decompression bombs

EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"} | ({".heic", ".heif"} if HEIC_OK else set())
RAW_EXTS = {".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".orf", ".rw2"}

# Adobe Stock category ids for its metadata CSV.
ADOBE_CATEGORIES = {
    "animals": 1, "buildings and architecture": 2, "business": 3, "drinks": 4, "the environment": 5,
    "states of mind": 6, "food": 7, "graphic resources": 8, "hobbies and leisure": 9, "industry": 10,
    "landscapes": 11, "lifestyle": 12, "people": 13, "plants and flowers": 14, "culture and religion": 15,
    "science": 16, "social issues": 17, "sports": 18, "technology": 19, "transport": 20, "travel": 21,
}

VISION_PROMPT = """You are screening a personal photo for sale on a stock photo site.
Look at the image and answer with ONLY a JSON object with these keys:
"aesthetic": integer 1-10 (composition, light, colour, subject; 7+ = genuinely good),
"stock_appeal": integer 1-10 (how likely a designer or publisher would license it; generic snapshots score low),
"title": short factual English title, max 70 characters, no brand names,
"description": one sentence describing what is shown,
"keywords": list of 25 to 45 single words or short phrases, most important first, no brand names,
"category": one of: """ + ", ".join(ADOBE_CATEGORIES) + """,
"recognizable_people": true if any person's face or identity could be recognised,
"private_property": true if it shows a private home, its interior, or identifiable private property,
"logos_or_brands": true if any logo, brand name, trademark or product branding is visible,
"license_plates": true if a readable vehicle licence plate is visible,
"artwork": true if a painting, mural, sculpture, poster, tattoo or other copyrighted artwork is a main part of the image,
"text_visible": true if readable text or signage is visible,
"notes": short reason for the scores.
"""


@dataclass
class Photo:
    path: str
    size_bytes: int = 0
    mtime: float = 0.0
    width: int = 0
    height: int = 0
    megapixels: float = 0.0
    sharpness: float = 0.0
    brightness: float = 0.0
    clipped_dark: float = 0.0
    clipped_bright: float = 0.0
    contrast: float = 0.0
    colorfulness: float = 0.0
    dhash: str = ""
    camera: str = ""
    taken: str = ""
    has_gps: bool = False
    tech_score: float = 0.0
    reject_reason: str = ""
    dup_of: str = ""
    error: str = ""
    vision: dict = field(default_factory=dict)
    final_score: float = 0.0
    action: str = ""
    release_flags: str = ""
    export_name: str = ""


# ---------------------------------------------------------------- technical pass

def _laplacian_var(gray: np.ndarray) -> float:
    g = gray.astype(np.float32)
    lap = (-4 * g[1:-1, 1:-1] + g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:])
    return float(lap.var())


def _dhash(img: Image.Image, size: int = 8) -> str:
    small = np.asarray(img.convert("L").resize((size + 1, size), Image.LANCZOS), dtype=np.int16)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    return "%016x" % int("".join("1" if b else "0" for b in bits), 2)


def _hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def _exif(img: Image.Image) -> tuple[str, str, bool]:
    try:
        ex = img.getexif()
    except Exception:
        return "", "", False
    camera = " ".join(str(ex.get(t, "")).strip() for t in (271, 272)).strip()  # Make, Model
    taken = str(ex.get(306, "") or "")  # DateTime
    try:
        sub = ex.get_ifd(0x8769)
        taken = str(sub.get(36867, taken) or taken)  # DateTimeOriginal
    except Exception:
        pass
    try:
        has_gps = bool(ex.get_ifd(0x8825))
    except Exception:
        has_gps = 0x8825 in ex
    return camera, taken, has_gps


def analyse(path: str) -> Photo:
    p = Photo(path=path)
    try:
        st = os.stat(path)
        p.size_bytes, p.mtime = st.st_size, st.st_mtime
        with Image.open(path) as im:  # opened read-only; nothing is ever written back
            p.camera, p.taken, p.has_gps = _exif(im)
            im = ImageOps.exif_transpose(im)
            p.width, p.height = im.size
            p.megapixels = round(p.width * p.height / 1e6, 2)
            work = im.convert("RGB")
            work.thumbnail((1024, 1024), Image.LANCZOS)
        p.dhash = _dhash(work)
        rgb = np.asarray(work, dtype=np.float32)
        gray = rgb @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
        p.sharpness = round(_laplacian_var(gray), 1)
        p.brightness = round(float(gray.mean()), 1)
        p.clipped_dark = round(float((gray < 8).mean()), 3)
        p.clipped_bright = round(float((gray > 247).mean()), 3)
        p.contrast = round(float(gray.std()), 1)
        rg = rgb[..., 0] - rgb[..., 1]
        yb = 0.5 * (rgb[..., 0] + rgb[..., 1]) - rgb[..., 2]
        p.colorfulness = round(float(np.hypot(rg.std(), yb.std()) + 0.3 * np.hypot(rg.mean(), yb.mean())), 1)
    except Exception as e:  # unreadable / corrupt file
        p.error = f"{type(e).__name__}: {e}"[:200]
    return p


def tech_score(p: Photo) -> float:
    """0-100. Rough, but good at pushing blurry / dark / tiny shots to the bottom."""
    if p.error:
        return 0.0
    sharp = min(1.0, max(0.0, (np.log10(p.sharpness + 1) - 1.3) / 1.4))     # ~20 -> 0, ~500 -> 1
    expo = 1.0 - min(1.0, abs(p.brightness - 125) / 110)
    clip = 1.0 - min(1.0, (p.clipped_dark + p.clipped_bright) * 4)
    cont = min(1.0, p.contrast / 60)
    col = min(1.0, p.colorfulness / 60)
    res = min(1.0, p.megapixels / 12)
    return round(100 * (0.35 * sharp + 0.15 * expo + 0.15 * clip + 0.12 * cont + 0.08 * col + 0.15 * res), 1)


def reject_reason(p: Photo, a: argparse.Namespace) -> str:
    if p.error:
        return "unreadable"
    if p.megapixels < a.min_mp:
        return f"too small ({p.megapixels} MP < {a.min_mp})"
    if p.brightness < 25 or p.brightness > 235:
        return "badly exposed"
    if p.clipped_dark + p.clipped_bright > 0.35:
        return "heavily clipped"
    if p.sharpness < a.min_sharpness:
        return f"blurry (sharpness {p.sharpness:.0f} < {a.min_sharpness})"
    return ""


def mark_duplicates(photos: list[Photo], max_dist: int) -> list[tuple[str, str, int]]:
    """Greedy clustering on dHash; best tech_score in each cluster survives."""
    pairs = []
    keepers: list[Photo] = []
    for p in sorted((x for x in photos if not x.reject_reason), key=lambda x: -x.tech_score):
        hit = next((k for k in keepers if _hamming(k.dhash, p.dhash) <= max_dist), None)
        if hit is None:
            keepers.append(p)
        else:
            p.dup_of = hit.path
            p.reject_reason = "near-duplicate"
            pairs.append((p.path, hit.path, _hamming(hit.dhash, p.dhash)))
    return pairs


# ---------------------------------------------------------------- vision pass (local ollama)

def _b64_jpeg(path: str, long_side: int = 1024) -> str:
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((long_side, long_side), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=88)  # in-memory copy only
    return base64.b64encode(buf.getvalue()).decode()


def parse_vision(text: str) -> dict:
    """Pull the JSON object out of a model reply and normalise types."""
    s, e = text.find("{"), text.rfind("}")
    if s < 0 or e <= s:
        raise ValueError("no JSON in model reply")
    d = json.loads(text[s:e + 1])

    def num(k: str) -> int:
        try:
            return max(1, min(10, int(round(float(d.get(k, 0))))))
        except (TypeError, ValueError):
            return 1

    def flag(k: str) -> bool:
        v = d.get(k, False)
        return v if isinstance(v, bool) else str(v).strip().lower() in {"true", "yes", "1"}

    kws = d.get("keywords") or []
    if isinstance(kws, str):
        kws = [k for k in kws.replace(";", ",").split(",")]
    seen, clean = set(), []
    for k in kws:
        k = str(k).strip().lower()
        if k and k not in seen:
            seen.add(k)
            clean.append(k)
    cat = str(d.get("category", "")).strip().lower()
    return {
        "aesthetic": num("aesthetic"),
        "stock_appeal": num("stock_appeal"),
        "title": str(d.get("title", "")).strip()[:70],
        "description": str(d.get("description", "")).strip()[:200],
        "keywords": clean[:49],
        "category": cat if cat in ADOBE_CATEGORIES else "",
        "recognizable_people": flag("recognizable_people"),
        "private_property": flag("private_property"),
        "logos_or_brands": flag("logos_or_brands"),
        "license_plates": flag("license_plates"),
        "artwork": flag("artwork"),
        "text_visible": flag("text_visible"),
        "notes": str(d.get("notes", "")).strip()[:200],
    }


def ask_vision(path: str, a: argparse.Namespace) -> dict:
    body = json.dumps({
        "model": a.model, "prompt": VISION_PROMPT, "images": [_b64_jpeg(path)],
        "stream": False, "format": "json", "options": {"temperature": 0.2},
        "keep_alive": "10m",
    }).encode()
    req = urllib.request.Request(a.ollama_url.rstrip("/") + "/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    last = None
    for _ in range(2):
        try:
            with urllib.request.urlopen(req, timeout=a.vision_timeout) as r:
                return parse_vision(json.loads(r.read())["response"])
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as e:
            last = e
    raise RuntimeError(f"vision failed: {last}")


def ollama_ready(a: argparse.Namespace) -> str:
    try:
        with urllib.request.urlopen(a.ollama_url.rstrip("/") + "/api/tags", timeout=5) as r:
            names = [m.get("name", "") for m in json.loads(r.read()).get("models", [])]
    except Exception as e:
        return f"ollama not reachable at {a.ollama_url} ({e})"
    base = a.model.split(":")[0]
    if not any(n == a.model or n.split(":")[0] == base for n in names):
        return f"model {a.model} not pulled (run: ollama pull {a.model})"
    return ""


# ---------------------------------------------------------------- scoring + outputs

RISK_KEYS = [("recognizable_people", "model release"), ("private_property", "property release"),
             ("logos_or_brands", "logo/brand"), ("license_plates", "licence plate"), ("artwork", "artwork")]


def finalise(p: Photo) -> None:
    v = p.vision
    if v and not v.get("error"):
        p.final_score = round(0.4 * p.tech_score + 3.0 * v["aesthetic"] + 3.0 * v["stock_appeal"], 1)
        flags = [label for key, label in RISK_KEYS if v.get(key)]
        p.release_flags = "; ".join(flags)
        if v["stock_appeal"] <= 3:
            p.action = "SKIP (low stock appeal)"
        elif flags:
            p.action = "NEEDS RELEASE / EDITORIAL ONLY"
        else:
            p.action = "SUBMIT"
    else:
        p.final_score = round(p.tech_score, 1)
        p.action = "REVIEW (no vision pass)"
    if p.has_gps:
        p.action += " | strip GPS on export"


def write_outputs(out: Path, photos: list[Photo], short: list[Photo], dups, a) -> None:
    cols = ["rank", "action", "final_score", "tech_score", "aesthetic", "stock_appeal", "release_flags",
            "title", "keywords", "category", "path", "megapixels", "sharpness", "camera", "taken", "has_gps", "notes"]
    with open(out / "shortlist.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for i, p in enumerate(short, 1):
            v = p.vision or {}
            w.writerow([i, p.action, p.final_score, p.tech_score, v.get("aesthetic", ""), v.get("stock_appeal", ""),
                        p.release_flags, v.get("title", ""), ", ".join(v.get("keywords", [])), v.get("category", ""),
                        p.path, p.megapixels, p.sharpness, p.camera, p.taken, p.has_gps, v.get("notes", v.get("error", ""))])

    with open(out / "all_scored.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["path", "tech_score", "reject_reason", "dup_of", "megapixels", "sharpness", "brightness",
                    "clipped_dark", "clipped_bright", "contrast", "colorfulness", "camera", "taken", "has_gps", "error"])
        for p in sorted(photos, key=lambda x: -x.tech_score):
            w.writerow([p.path, p.tech_score, p.reject_reason, p.dup_of, p.megapixels, p.sharpness, p.brightness,
                        p.clipped_dark, p.clipped_bright, p.contrast, p.colorfulness, p.camera, p.taken, p.has_gps, p.error])

    with open(out / "duplicates.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["duplicate", "kept", "hash_distance"])
        w.writerows(dups)

    if a.export:
        ready = [p for p in short if p.action.startswith("SUBMIT")]
        n = export_copies(out / "upload", ready, enhance=not a.no_enhance)
        print(f"exported {n} upload-ready copies to {out / 'upload'}")

    # Adobe Stock lets you upload a CSV of titles/keywords after the files are uploaded.
    with open(out / "adobe_stock_metadata.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Filename", "Title", "Keywords", "Category", "Releases"])
        for p in short:
            v = p.vision or {}
            if not p.action.startswith("SUBMIT"):
                continue
            w.writerow([p.export_name or Path(p.path).stem + ".jpg", v.get("title", ""), ", ".join(v.get("keywords", [])),
                        ADOBE_CATEGORIES.get(v.get("category", ""), ""), ""])

    contact_sheet(out / "contact_sheet.jpg", short[: a.sheet])

    counts: dict[str, int] = {}
    for p in photos:
        key = p.reject_reason.split(" (")[0] if p.reject_reason else "kept"
        counts[key] = counts.get(key, 0) + 1
    n_submit = sum(1 for p in short if p.action.startswith("SUBMIT"))
    n_rel = sum(1 for p in short if p.action.startswith("NEEDS"))
    lines = [f"AURIX photo scout - {time.strftime('%Y-%m-%d %H:%M')}",
             f"source: {', '.join(a.src)}", f"scanned: {len(photos)}"]
    lines += [f"  {k}: {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]
    lines += [f"shortlist: {len(short)} (submit-ready {n_submit}, need release/editorial {n_rel})",
              "nothing in the source folder was changed."]
    (out / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


def export_copies(dest: Path, photos: list[Photo], enhance: bool) -> int:
    """Write upload-ready COPIES (originals untouched): sRGB JPEG, no EXIF/GPS, optional light polish.

    The polish is deliberately mild (auto-levels, gentle sharpening, +5% colour). Stock reviewers
    reject over-processed or upscaled images, so this never resizes up and never adds detail
    that is not in the photo.
    """
    dest.mkdir(parents=True, exist_ok=True)
    used, n = set(), 0
    for p in photos:
        stem = Path(p.path).stem
        name, k = f"{stem}.jpg", 1
        while name.lower() in used:
            k += 1
            name = f"{stem}_{k}.jpg"
        used.add(name.lower())
        try:
            with Image.open(p.path) as im:
                icc = im.info.get("icc_profile")
                im = ImageOps.exif_transpose(im)
                if icc and ImageCms is not None:  # e.g. iPhone Display P3 -> sRGB
                    try:
                        src_prof = ImageCms.ImageCmsProfile(io.BytesIO(icc))
                        im = ImageCms.profileToProfile(im.convert("RGB"), src_prof, ImageCms.createProfile("sRGB"),
                                                       outputMode="RGB")
                    except Exception:
                        pass
                im = im.convert("RGB")
            if enhance:
                im = ImageOps.autocontrast(im, cutoff=0.3, preserve_tone=True)
                im = im.filter(ImageFilter.UnsharpMask(radius=1.2, percent=60, threshold=3))
                im = ImageEnhance.Color(im).enhance(1.05)
            srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes() if ImageCms else None
            im.save(dest / name, "JPEG", quality=95, subsampling=0, icc_profile=srgb)  # no exif => no GPS
            p.export_name = name
            n += 1
        except Exception as e:
            p.export_name = ""
            print(f"  export failed for {p.path}: {e}")
    return n


def contact_sheet(dest: Path, photos: list[Photo], cell: int = 300, cols: int = 6) -> None:
    if not photos:
        return
    rows = (len(photos) + cols - 1) // cols
    label_h = 46
    sheet = Image.new("RGB", (cols * cell, rows * (cell + label_h)), (18, 18, 22))
    d = ImageDraw.Draw(sheet)
    for i, p in enumerate(photos):
        x, y = (i % cols) * cell, (i // cols) * (cell + label_h)
        try:
            with Image.open(p.path) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")
                im.thumbnail((cell - 8, cell - 8), Image.LANCZOS)
                sheet.paste(im, (x + (cell - im.width) // 2, y + (cell - im.height) // 2))
        except Exception:
            d.text((x + 10, y + 10), "unreadable", fill=(255, 80, 80))
        colour = (90, 220, 120) if p.action.startswith("SUBMIT") else (
            (255, 190, 60) if p.action.startswith("NEEDS") else (180, 180, 180))
        d.rectangle([x + 2, y + 2, x + cell - 3, y + cell - 3], outline=colour, width=3)
        d.text((x + 6, y + cell + 2), f"#{i + 1}  {p.final_score:.0f}  {p.action.split(' |')[0][:26]}", fill=colour)
        d.text((x + 6, y + cell + 22), Path(p.path).name[:40], fill=(200, 200, 200))
    sheet.save(dest, "JPEG", quality=85)


# ---------------------------------------------------------------- main

def scan(src: Path, out: Path) -> tuple[list[str], int]:
    files, raws = [], 0
    out_r = out.resolve()
    for root, dirs, names in os.walk(src):
        rp = Path(root).resolve()
        if rp == out_r or out_r in rp.parents:
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for n in names:
            ext = Path(n).suffix.lower()
            if ext in EXTS and not n.startswith("."):
                files.append(str(Path(root) / n))
            elif ext in RAW_EXTS:
                raws += 1
    return sorted(files), raws


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--src", nargs="+", default=[os.environ["AURIX_PHOTO_DIR"]] if os.environ.get("AURIX_PHOTO_DIR") else None,
                    help="one or more photo folders (read-only), e.g. an Immich library")
    ap.add_argument("--out", default=os.environ.get("AURIX_PHOTO_OUT", "photo_scout_out"))
    ap.add_argument("--model", default=os.environ.get("AURIX_VISION_MODEL", "qwen2.5vl:3b"))
    ap.add_argument("--ollama-url", default=os.environ.get("OLLAMA_HOST", "http://localhost:11434"))
    ap.add_argument("--no-vision", action="store_true", help="technical pass only")
    ap.add_argument("--vision-top", type=int, default=80, help="how many survivors get the vision pass")
    ap.add_argument("--vision-timeout", type=int, default=180)
    ap.add_argument("--shortlist", type=int, default=40)
    ap.add_argument("--sheet", type=int, default=36, help="photos on the contact sheet")
    ap.add_argument("--min-mp", type=float, default=4.0, help="stock sites want 4 MP or more")
    ap.add_argument("--min-sharpness", type=float, default=60.0)
    ap.add_argument("--dup-distance", type=int, default=6, help="dHash bits; higher = more aggressive")
    ap.add_argument("--export", action="store_true",
                    help="write upload-ready JPEG copies of SUBMIT shots to OUT/upload (originals untouched)")
    ap.add_argument("--no-enhance", action="store_true", help="export without the light polish")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    a = ap.parse_args(argv)

    if not a.src:
        ap.error("--src is required (or set AURIX_PHOTO_DIR)")
    srcs = [Path(x).expanduser().resolve() for x in a.src]
    out = Path(a.out).expanduser().resolve()
    for src in srcs:
        if not src.is_dir():
            ap.error(f"not a folder: {src}")
        if out == src or src in out.parents:
            # never write inside a photo library (Immich, for one, must not have stray files added)
            ap.error(f"--out must be outside every --src folder ({out} is inside {src})")
    out.mkdir(parents=True, exist_ok=True)

    files, raws = [], 0
    for src in srcs:
        f, r = scan(src, out)
        files += f
        raws += r
    files = sorted(set(files))
    where = ", ".join(map(str, srcs))
    print(f"found {len(files)} images in {where}" + (f" (skipped {raws} RAW files; export them to JPEG first)" if raws else ""))
    if not files:
        return 1

    cache_path = out / "cache.json"
    try:
        cache = json.loads(cache_path.read_text())
    except Exception:
        cache = {}

    photos: list[Photo] = []
    todo = []
    for f in files:
        st = os.stat(f)
        c = cache.get(f)
        if c and c.get("size_bytes") == st.st_size and c.get("mtime") == st.st_mtime:
            photos.append(Photo(**{k: v for k, v in c.items() if k in Photo.__dataclass_fields__}))
        else:
            todo.append(f)
    print(f"technical pass: {len(todo)} new, {len(photos)} cached")
    t0 = time.time()
    if todo:
        with ProcessPoolExecutor(max_workers=a.workers) as ex:
            for i, p in enumerate(ex.map(analyse, todo, chunksize=8), 1):
                photos.append(p)
                if i % 200 == 0:
                    print(f"  {i}/{len(todo)}  ({time.time() - t0:.0f}s)")

    for p in photos:
        p.reject_reason, p.dup_of = "", ""
        p.tech_score = tech_score(p)
        p.reject_reason = reject_reason(p, a)
    dups = mark_duplicates(photos, a.dup_distance)
    survivors = sorted((p for p in photos if not p.reject_reason), key=lambda x: -x.tech_score)
    print(f"survivors after blur/size/exposure/duplicate filters: {len(survivors)}")

    vision_set = survivors[: a.vision_top]
    if not a.no_vision:
        problem = ollama_ready(a)
        if problem:
            print(f"vision pass skipped: {problem}")
        else:
            for i, p in enumerate(vision_set, 1):
                if p.vision and not p.vision.get("error") and p.vision.get("_model") == a.model:
                    continue
                try:
                    p.vision = ask_vision(p.path, a)
                    p.vision["_model"] = a.model
                except Exception as e:
                    p.vision = {"error": str(e)[:200]}
                print(f"  vision {i}/{len(vision_set)}  {Path(p.path).name}  "
                      f"{p.vision.get('aesthetic', '-')}/{p.vision.get('stock_appeal', '-')}")
                cache[p.path] = asdict(p)
                if i % 10 == 0:
                    cache_path.write_text(json.dumps(cache))

    for p in photos:
        cache[p.path] = asdict(p)
    cache_path.write_text(json.dumps(cache))

    for p in vision_set:
        finalise(p)
    order = {"SUBMIT": 0, "NEEDS": 1, "REVIEW": 2, "SKIP": 3}
    short = sorted(vision_set, key=lambda p: (order.get(p.action.split()[0], 9), -p.final_score))[: a.shortlist]
    write_outputs(out, photos, short, dups, a)
    print(f"outputs in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
