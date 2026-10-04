#!/usr/bin/env python3
"""cad_build.py - a parametric part from a build123d script -> printable STL + report.json (the idea of
nazirlouis/ada_v2's CAD agent, MIT: describe a part, get an editable model, not a mesh guessed by an AI).

    python3 cad_build.py --script part.py --output part.stl --report report.json [--bed 220x220x250]
    python3 cad_build.py --selftest        # builds a bracket; proves build123d + trimesh work

The script is ordinary build123d code and must leave the finished solid in a variable named `result`, e.g.

    from build123d import *
    with BuildPart() as p:
        Box(40, 20, 5)
        with Locations((0, 0, 2.5)):
            Hole(radius=3)
    result = p.part

Units are millimetres. The report has the same keys as ct_to_stl.py (watertight, volume_cm3, area_cm2, extents_mm), so
print_cost.py prices it unchanged, plus `fits_bed` and `warnings`. Runs inside the offline sandbox; nothing is uploaded.
"""
import argparse
import json
import os
import sys
import time

DEFAULT_BED_MM = (220.0, 220.0, 250.0)        # a common FDM bed; override with --bed or AURIX_PRINTER_BED_MM
MIN_FEATURE_MM = 0.8                          # anything thinner than a couple of nozzle widths will not print well


# ---------------------------------------------------------------------------
# pure decisions (unit-tested without build123d)
# ---------------------------------------------------------------------------

def parse_bed(text):
    """'220x220x250' -> (220.0, 220.0, 250.0). Raises ValueError on anything else."""
    parts = [float(x) for x in str(text).lower().replace(" ", "").split("x")]
    if len(parts) != 3 or any(p <= 0 or p > 2000 for p in parts):
        raise ValueError("bed must be WxDxH in mm, e.g. 220x220x250")
    return tuple(parts)


def fits_bed(extents, bed):
    """True when the part fits the bed in its current orientation or lying on any face (sorted comparison)."""
    return all(e <= b for e, b in zip(sorted(extents), sorted(bed)))


def build_report(extents, volume_mm3, area_mm2, watertight, bed, seconds=0.0):
    ext = [round(float(e), 1) for e in extents]
    warnings = []
    if not watertight:
        warnings.append("mesh is not watertight: the slicer may fill or skip parts of it")
    if min(ext) < MIN_FEATURE_MM:
        warnings.append(f"thinnest extent is {min(ext)} mm: thinner than ~{MIN_FEATURE_MM} mm will not print reliably")
    fit = fits_bed(ext, bed)
    if not fit:
        warnings.append(f"does not fit a {bed[0]:g}x{bed[1]:g}x{bed[2]:g} mm bed in any orientation")
    return {"tool": "cad_build", "watertight": bool(watertight),
            "volume_cm3": round(abs(float(volume_mm3)) / 1000.0, 3) if watertight else None,
            "area_cm2": round(float(area_mm2) / 100.0, 2), "extents_mm": ext, "fits_bed": fit,
            "bed_mm": list(bed), "warnings": warnings, "seconds": round(seconds, 2)}


def load_result(script_text, filename="part.py"):
    """Run the build123d script and return its `result`. The sandbox is the isolation boundary."""
    scope = {"__name__": "__cad__", "__file__": filename}
    exec(compile(script_text, filename, "exec"), scope)                  # noqa: S102 - offline sandbox, owner-approved lane
    if "result" not in scope:
        raise ValueError("the script must assign the finished solid to a variable named `result`")
    return scope["result"]


# ---------------------------------------------------------------------------
# the build (needs build123d + trimesh: the sandbox image's optional CAD layer)
# ---------------------------------------------------------------------------

def build(script_path, out_stl, bed):
    t0 = time.time()
    from build123d import export_stl                                   # optional layer: a clear error if absent
    import trimesh
    with open(script_path, encoding="utf-8") as f:
        result = load_result(f.read(), os.path.basename(script_path))
    shape = getattr(result, "part", result)                             # accept a BuildPart context or a Part
    export_stl(shape, out_stl)
    mesh = trimesh.load(out_stl, force="mesh")
    lo = mesh.bounds[0]
    if abs(lo[2]) > 1e-6:                                                # sit it on the bed (z = 0) for the slicer
        mesh.apply_translation([0, 0, -lo[2]])
        mesh.export(out_stl)
    return build_report(mesh.extents, mesh.volume, mesh.area, mesh.is_watertight, bed, time.time() - t0)


SELFTEST_SCRIPT = """
from build123d import *
with BuildPart() as p:
    Box(40, 20, 5)
    with Locations((10, 0, 0)):
        Hole(radius=3)
result = p.part
"""


def selftest():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        script, stl = os.path.join(d, "part.py"), os.path.join(d, "part.stl")
        with open(script, "w", encoding="utf-8") as f:
            f.write(SELFTEST_SCRIPT)
        rep = build(script, stl, DEFAULT_BED_MM)
    ok = rep["watertight"] and rep["extents_mm"] == [40.0, 20.0, 5.0] and rep["fits_bed"]
    print(json.dumps(rep, indent=2))
    print("SELFTEST " + ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--script")
    ap.add_argument("--output")
    ap.add_argument("--report")
    ap.add_argument("--bed", default=os.environ.get("AURIX_PRINTER_BED_MM", ""))
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if not (a.script and a.output):
        ap.error("--script and --output are required")
    try:
        bed = parse_bed(a.bed) if a.bed else DEFAULT_BED_MM
        rep = build(a.script, a.output, bed)
    except ImportError as e:
        print(f"ERROR: the CAD layer is not installed in this sandbox image ({e}); rebuild with the CAD layer", file=sys.stderr)
        return 2
    except Exception as e:                                              # a broken script is the common case: say why
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    if a.report:
        with open(a.report, "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=2)
    print(json.dumps(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
