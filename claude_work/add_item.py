#!/usr/bin/env python3
"""Add one piece of Claude's work to the library.

  python3 add_item.py --id my-thing --title "My thing" --kind script \
      --note "One line on what it is." FILE [FILE ...] [--thumb cover.png] [--external big.zip]

Kinds: pack, art, spec, checklist, script (anything else shows in "all").
Files are copied into items/<id>/; --external files are only listed, not copied.
Re-running with the same --id replaces that entry.
"""
import argparse, datetime, json, shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent

ap = argparse.ArgumentParser()
ap.add_argument("--id", required=True)
ap.add_argument("--title", required=True)
ap.add_argument("--kind", required=True)
ap.add_argument("--note", default="")
ap.add_argument("--thumb")
ap.add_argument("--external", action="append", default=[])
ap.add_argument("files", nargs="*")
a = ap.parse_args()

dest = ROOT / "items" / a.id
dest.mkdir(parents=True, exist_ok=True)
files = []
for f in a.files + ([a.thumb] if a.thumb and a.thumb not in a.files else []):
    p = Path(f)
    target = dest / p.name
    (shutil.copytree(p, target, dirs_exist_ok=True) if p.is_dir() else shutil.copy2(p, target))
    files.append(f"items/{a.id}/{p.name}" + ("/" if p.is_dir() else ""))

entry = dict(id=a.id, title=a.title, kind=a.kind, date=datetime.date.today().isoformat(), note=a.note,
             path=f"items/{a.id}/", files=files,
             external=[dict(name=Path(e).name, path=e, mb=round(Path(e).stat().st_size / 1e6, 1))
                       for e in a.external if Path(e).exists()])
if a.thumb:
    entry["thumb"] = f"items/{a.id}/{Path(a.thumb).name}"

mf = ROOT / "manifest.json"
m = json.loads(mf.read_text()) if mf.exists() else {"version": 1, "items": []}
m["items"] = [entry] + [i for i in m["items"] if i["id"] != a.id]
m["updated"] = entry["date"]
mf.write_text(json.dumps(m, indent=2) + "\n")
print(f"added {a.id} ({len(files)} files)")
