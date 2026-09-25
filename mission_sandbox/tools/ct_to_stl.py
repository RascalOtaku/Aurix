#!/usr/bin/env python3
"""ct_to_stl.py - CT scan (DICOM series or NIfTI/MHA) -> clean, watertight, printable STL.

    python3 ct_to_stl.py --input /work/scan_dir --output /work/skull.stl [--threshold 300]
                         [--smooth 0.8] [--target-faces 300000] [--iso-mm 0.8]
                         [--report report.json] [--preview preview.png] [--series-id ID]
    python3 ct_to_stl.py --selftest        # synthetic skull; proves the toolchain works

Pipeline: load series (Hounsfield units, real voxel spacing) -> threshold bone -> remove specks ->
keep the largest connected component (drops the scanner table/headrest) -> light smoothing ->
marching cubes (in millimetres) -> mesh repair (merge, degenerate/duplicate faces, holes,
normals) -> Taubin smoothing (volume-preserving) -> optional decimation -> place on the bed ->
validate (watertight? volume, extents) -> export binary STL + report.json.

The report feeds print_cost.py. IMPORTANT: models from CT are for education/visualisation, NOT
clinical or surgical use. Patient identifiers are never written to any output.
"""
import argparse
import json
import math
import os
import sys
import time

DEFAULT_THRESHOLD_HU = 300
DEFAULT_TARGET_FACES = 300_000


# ---------------------------------------------------------------------------
# pure decisions (unit-tested without any imaging library)
# ---------------------------------------------------------------------------

def choose_series(counts):
    """DICOM directories often hold scouts/localizers too: take the series with the most slices.
    `counts` maps series id -> number of files. Ties resolve to the lowest id (deterministic)."""
    if not counts:
        raise ValueError("no DICOM series found")
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def hu_warnings(hu_min, hu_max, threshold):
    w = []
    if hu_max < threshold:
        w.append(f"no voxel reaches the bone threshold ({threshold} HU; max is {hu_max:.0f}): "
                 "this may not be a CT, or the values are not in Hounsfield units")
    if hu_min > -200:
        w.append(f"minimum value is {hu_min:.0f}: air should be about -1000 HU; check the series")
    if hu_max > 10000:
        w.append(f"maximum value is {hu_max:.0f} HU: metal artefacts or bad scaling are likely")
    return w


def spacing_warnings(spacing_xyz):
    sx, sy, sz = spacing_xyz
    w = []
    if min(spacing_xyz) <= 0:
        raise ValueError("non-positive voxel spacing")
    if sz > 3.0:
        w.append(f"slice spacing is {sz:.1f} mm: thick slices give stair-stepped surfaces")
    if max(spacing_xyz) / min(spacing_xyz) > 3.0:
        w.append("voxels are strongly anisotropic; consider --iso-mm to resample")
    return w


def plan_resample(spacing_xyz, iso_mm):
    """New isotropic spacing to resample to, or None if not requested / already ~isotropic."""
    if not iso_mm:
        return None
    if all(abs(s - iso_mm) / iso_mm < 0.05 for s in spacing_xyz):
        return None
    return (float(iso_mm),) * 3


def face_budget(requested):
    """Faces to decimate to (0/None = default cap; negative = no decimation)."""
    if requested is None or requested == 0:
        return DEFAULT_TARGET_FACES
    return None if requested < 0 else int(requested)


def keep_bodies(face_counts, min_fraction=0.01):
    """Indices of the mesh bodies worth keeping: everything at least `min_fraction` the size (in faces) of the
    largest body. Debris is tiny; a cavity wall is comparable to the outer surface, so it survives."""
    if not face_counts:
        raise ValueError("mesh has no bodies")
    top = max(face_counts)
    return [i for i, n in enumerate(face_counts) if n >= top * min_fraction]


def build_report(meta, params, stats, warnings, elapsed_s):
    return {"tool": "ct_to_stl", "purpose": "education/visualisation - not for clinical or surgical use",
            "source": meta, "parameters": params, "mesh": stats,
            "volume_cm3": stats.get("volume_cm3"), "area_cm2": stats.get("area_cm2"),
            "extents_mm": stats.get("extents_mm"), "watertight": stats.get("watertight"),
            "warnings": warnings, "elapsed_s": round(elapsed_s, 1)}


# ---------------------------------------------------------------------------
# imaging (needs numpy, scipy, scikit-image, SimpleITK, trimesh: present in the sandbox image)
# ---------------------------------------------------------------------------

def load_volume(path, series_id=None):
    """-> (array[z,y,x] float32 in HU, spacing (x,y,z) mm, meta). No identifying tags are returned."""
    import SimpleITK as sitk
    warn = []
    if os.path.isdir(path):
        reader = sitk.ImageSeriesReader()
        ids = reader.GetGDCMSeriesIDs(path)
        if not ids:
            raise ValueError(f"no DICOM series found in {path}")
        counts = {i: len(reader.GetGDCMSeriesFileNames(path, i)) for i in ids}
        sid = series_id or choose_series(counts)
        files = reader.GetGDCMSeriesFileNames(path, sid)
        reader.SetFileNames(files)
        image = reader.Execute()
        if len(ids) > 1:
            warn.append(f"{len(ids)} series in the folder; used the one with the most slices ({counts[sid]})")
        modality = None
        try:
            import pydicom
            modality = str(pydicom.dcmread(files[0], stop_before_pixels=True).get("Modality", ""))
        except Exception:
            pass
    else:
        image, modality = sitk.ReadImage(path), None
    if image.GetDimension() != 3:
        raise ValueError("input is not a 3D volume")
    if modality and modality != "CT":
        warn.append(f"modality is {modality}, not CT: bone thresholds in HU will not apply")
    if any(abs(a - b) > 1e-3 for a, b in zip(image.GetDirection(), (1, 0, 0, 0, 1, 0, 0, 0, 1))):
        warn.append("oblique/tilted acquisition (gantry tilt): geometry is not re-aligned to patient axes")
    arr = sitk.GetArrayFromImage(sitk.Cast(image, sitk.sitkFloat32))
    spacing = tuple(float(s) for s in image.GetSpacing())
    meta = {"slices": int(arr.shape[0]), "size_zyx": [int(s) for s in arr.shape], "spacing_xyz_mm": list(spacing),
            "modality": modality, "hu_min": float(arr.min()), "hu_max": float(arr.max())}
    return arr, spacing, meta, warn


def resample_volume(arr, spacing_xyz, new_spacing):
    import SimpleITK as sitk
    img = sitk.GetImageFromArray(arr)
    img.SetSpacing(spacing_xyz)
    size = [int(round(n * s / ns)) for n, s, ns in zip(img.GetSize(), spacing_xyz, new_spacing)]
    out = sitk.Resample(img, size, sitk.Transform(), sitk.sitkLinear, img.GetOrigin(), new_spacing,
                        img.GetDirection(), float(arr.min()), sitk.sitkFloat32)
    return sitk.GetArrayFromImage(out), new_spacing


def mesh_from_volume(arr, spacing_xyz, threshold=DEFAULT_THRESHOLD_HU, smooth_sigma=0.8, keep_largest=True,
                     fill_holes=False):
    """Bone mask -> surface vertices/faces in millimetres (x, y, z order)."""
    import numpy as np
    from scipy import ndimage
    from skimage import measure

    mask = arr >= threshold
    if not mask.any():
        raise ValueError(f"nothing at or above {threshold} HU")
    mask = ndimage.binary_opening(mask, structure=ndimage.generate_binary_structure(3, 1))
    if keep_largest:
        labels, n = ndimage.label(mask, structure=np.ones((3, 3, 3)))
        if n == 0:
            raise ValueError("bone mask vanished after cleaning; lower --threshold")
        sizes = np.bincount(labels.ravel())
        sizes[0] = 0
        mask = labels == sizes.argmax()
    if fill_holes:        # off by default: it would solidify the cranial cavity
        mask = ndimage.binary_fill_holes(mask)
    vol = mask.astype(np.float32)
    if smooth_sigma and smooth_sigma > 0:
        vol = ndimage.gaussian_filter(vol, sigma=smooth_sigma)
    vol = np.pad(vol, 1)                                  # closes the surface at the volume edges
    spacing_zyx = (spacing_xyz[2], spacing_xyz[1], spacing_xyz[0])
    verts, faces, _n, _v = measure.marching_cubes(vol, level=0.5, spacing=spacing_zyx)
    verts = verts[:, ::-1] - np.array(spacing_xyz)        # (z,y,x)->(x,y,z), undo the 1-voxel pad
    return verts, faces


def clean_mesh(verts, faces, smooth_iters=10, target_faces=DEFAULT_TARGET_FACES, log=print):
    """Repair, smooth, decimate, and seat the mesh on the bed. Returns a trimesh.Trimesh."""
    import trimesh
    mesh = trimesh.Trimesh(vertices=verts, faces=faces, process=False)
    mesh.merge_vertices()
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.update_faces(mesh.unique_faces())
    mesh.remove_unreferenced_vertices()
    parts = mesh.split(only_watertight=False)
    multibody = False
    if len(parts) > 1:
        # Drop debris, but KEEP inner surfaces: the wall of a closed cavity (a skull's cranial vault, a hollow part)
        # is a separate connected body, and discarding it silently turns a shell into a solid.
        kept = [parts[i] for i in keep_bodies([len(p.faces) for p in parts])]
        multibody = len(kept) > 1
        mesh = trimesh.util.concatenate(kept) if multibody else kept[0]
    if multibody:
        # marching cubes already orients every shell consistently (outer surface outward, cavity walls facing the
        # cavity). fix_normals would flip each cavity wall "outward" and fill the cavity, so only check the total.
        if mesh.volume < 0:
            mesh.invert()
    else:
        trimesh.repair.fix_normals(mesh)
    trimesh.repair.fill_holes(mesh)
    if smooth_iters:
        try:
            trimesh.smoothing.filter_taubin(mesh, iterations=int(smooth_iters))
        except Exception as e:
            log(f"[warn] Taubin smoothing skipped: {e}")
    if target_faces and len(mesh.faces) > target_faces:
        try:
            import pymeshlab
            ms = pymeshlab.MeshSet()
            ms.add_mesh(pymeshlab.Mesh(vertex_matrix=mesh.vertices, face_matrix=mesh.faces))
            ms.meshing_decimation_quadric_edge_collapse(targetfacenum=int(target_faces), preservenormal=True,
                                                        preservetopology=True)
            m = ms.current_mesh()
            mesh = trimesh.Trimesh(vertices=m.vertex_matrix(), faces=m.face_matrix(), process=False)
        except Exception as e:
            log(f"[warn] decimation skipped ({e}); STL will be large")
    if not mesh.is_watertight:                            # one repair pass via MeshLab, then re-check
        try:
            import pymeshlab
            ms = pymeshlab.MeshSet()
            ms.add_mesh(pymeshlab.Mesh(vertex_matrix=mesh.vertices, face_matrix=mesh.faces))
            ms.meshing_repair_non_manifold_edges()
            ms.meshing_close_holes(maxholesize=200)
            m = ms.current_mesh()
            mesh = trimesh.Trimesh(vertices=m.vertex_matrix(), faces=m.face_matrix(), process=True)
        except Exception as e:
            log(f"[warn] MeshLab repair skipped: {e}")
    b = mesh.bounds
    mesh.apply_translation([-(b[0][0] + b[1][0]) / 2, -(b[0][1] + b[1][1]) / 2, -b[0][2]])   # centre, sit on z=0
    return mesh


def mesh_stats(mesh):
    return {"faces": int(len(mesh.faces)), "vertices": int(len(mesh.vertices)),
            "watertight": bool(mesh.is_watertight), "winding_consistent": bool(mesh.is_winding_consistent),
            "volume_cm3": round(abs(float(mesh.volume)) / 1000.0, 3) if mesh.is_watertight else None,
            "area_cm2": round(float(mesh.area) / 100.0, 2),
            "extents_mm": [round(float(e), 1) for e in mesh.extents]}


def write_preview(mesh, path):
    """Three orthographic silhouettes so a human can eyeball the result quickly."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    v = mesh.vertices
    fig, axes = plt.subplots(1, 3, figsize=(12, 4))
    for ax, (i, j, name) in zip(axes, ((0, 1, "top (x-y)"), (0, 2, "front (x-z)"), (1, 2, "side (y-z)"))):
        ax.scatter(v[::7, i], v[::7, j], s=0.2, c="k")
        ax.set_aspect("equal")
        ax.set_title(name)
    fig.savefig(path, dpi=110, bbox_inches="tight")
    plt.close(fig)


def run(input_path, output, threshold=DEFAULT_THRESHOLD_HU, smooth=0.8, target_faces=0, iso_mm=None,
        series_id=None, report_path=None, preview_path=None, fill_holes=False, log=print):
    started = time.time()
    arr, spacing, meta, warnings = load_volume(input_path, series_id)
    warnings += hu_warnings(meta["hu_min"], meta["hu_max"], threshold) + spacing_warnings(spacing)
    for w in warnings:
        log("[warn] " + w)
    new_spacing = plan_resample(spacing, iso_mm)
    if new_spacing:
        log(f"resampling {spacing} -> {new_spacing} mm")
        arr, spacing = resample_volume(arr, spacing, new_spacing)
    verts, faces = mesh_from_volume(arr, spacing, threshold, smooth, fill_holes=fill_holes)
    mesh = clean_mesh(verts, faces, target_faces=face_budget(target_faces), log=log)
    stats = mesh_stats(mesh)
    if not stats["watertight"]:
        warnings.append("mesh is NOT watertight after repair: slicers may fail; try a different --threshold or --smooth")
    mesh.export(output)
    import trimesh
    back = trimesh.load(output)                            # prove the file we wrote reads back
    stats["reload_faces"] = int(len(back.faces))
    params = {"threshold_hu": threshold, "smooth_sigma": smooth, "iso_mm": iso_mm, "target_faces": face_budget(target_faces),
              "fill_holes": fill_holes}
    report = build_report(meta, params, stats, warnings, time.time() - started)
    if report_path:
        json.dump(report, open(report_path, "w", encoding="utf-8"), indent=2)
    if preview_path:
        try:
            write_preview(mesh, preview_path)
        except Exception as e:
            log(f"[warn] preview skipped: {e}")
    return report


def _selftest():
    """Synthetic 'skull': a bone shell + an isolated bone speck + a separate table plate."""
    import numpy as np
    n = 110
    z, y, x = np.mgrid[:n, :n, :n].astype(np.float32)
    r = np.sqrt((z - 55) ** 2 + (y - 55) ** 2 + (x - 55) ** 2)
    arr = np.full((n, n, n), -1000.0, np.float32)
    arr[(r >= 30) & (r <= 34)] = 1200.0                              # the shell
    arr[3:6, 3:6, 3:6] = 1200.0                                      # a stray speck (must be dropped)
    arr[:, 104:108, :] = 1200.0                                      # the scanner table (must be dropped)
    verts, faces = mesh_from_volume(arr, (1.0, 1.0, 1.0), 300, 0.8)
    mesh = clean_mesh(verts, faces, smooth_iters=5, target_faces=None)
    stats = mesh_stats(mesh)
    analytic = 4.0 / 3.0 * math.pi * (34 ** 3 - 30 ** 3) / 1000.0
    assert stats["watertight"], stats
    assert abs(stats["volume_cm3"] - analytic) / analytic < 0.20, (stats["volume_cm3"], analytic)
    assert max(stats["extents_mm"]) < 80, stats                       # table/speck are gone
    print(f"ct_to_stl selftest OK: watertight, {stats['volume_cm3']:.1f} cm3 (analytic {analytic:.1f}), "
          f"extents {stats['extents_mm']} mm, {stats['faces']} faces")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input")
    ap.add_argument("--output")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD_HU, help="bone threshold in HU")
    ap.add_argument("--smooth", type=float, default=0.8, help="gaussian sigma in voxels (0 = none)")
    ap.add_argument("--target-faces", type=int, default=0, help="0 = default cap, -1 = no decimation")
    ap.add_argument("--iso-mm", type=float, default=None, help="resample to isotropic voxels of this size")
    ap.add_argument("--series-id")
    ap.add_argument("--report")
    ap.add_argument("--preview")
    ap.add_argument("--fill-holes", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    if not (a.input and a.output):
        ap.error("--input and --output are required")
    rep = run(a.input, a.output, a.threshold, a.smooth, a.target_faces, a.iso_mm, a.series_id, a.report,
              a.preview, a.fill_holes)
    m = rep["mesh"]
    print(f"wrote {a.output}: {m['faces']} faces, {rep['extents_mm']} mm, volume {rep['volume_cm3']} cm3, "
          f"watertight={rep['watertight']}")
    return 0 if rep["watertight"] else 3


if __name__ == "__main__":
    sys.exit(main())
