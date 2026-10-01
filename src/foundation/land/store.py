"""land/store.py - save dossiers (machine-readable JSON + human report) and audit every save.

Protected component (approval_gate): agent tools cannot write data/land/, so no agent can forge a dossier or an approval.

Layout, following the Foundation's data/<capability>/ convention:
    $AURIX_PROJECT_ROOT/data/land/dossiers/<property key>/<version[:12]>.json|.md
    $AURIX_PROJECT_ROOT/data/land/dossiers/<property key>/latest.json   (pointer: version + path)
Versions are content hashes, so files are never overwritten with different content; history is kept.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from src.foundation import audit
from src.foundation.land import report


def data_dir() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data" / "land"


def _atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def save(dossier: Dict[str, Any]) -> Dict[str, str]:
    key, ver = dossier["property"]["key"], dossier["version"]
    base = data_dir() / "dossiers" / key
    jpath, mpath = base / f"{ver[:12]}.json", base / f"{ver[:12]}.md"
    _atomic(jpath, json.dumps(dossier, indent=2, ensure_ascii=False, sort_keys=True))
    _atomic(mpath, report.render(dossier))
    _atomic(base / "latest.json", json.dumps({"version": ver, "json": jpath.name, "md": mpath.name}, indent=2))
    rec = audit.append("land_dossier_built", property=key, version=ver, verdict=dossier["recommendation"]["verdict"],
                       gate=dossier["recommendation"]["gate"].split(":")[0], max_usd=dossier["economics"]["cap_usd"],
                       mode=dossier["mode"], unknowns=len(dossier["unknowns"]), automation_level=dossier["automation_level"])
    if not rec:
        raise RuntimeError("audit append failed; dossier written but NOT audited - investigate before relying on it")
    return {"json": str(jpath), "md": str(mpath), "audit_seq": str(rec.get("seq"))}


def load_latest(key: str) -> Optional[Dict[str, Any]]:
    base = data_dir() / "dossiers" / key
    try:
        ptr = json.loads((base / "latest.json").read_text(encoding="utf-8"))
        return json.loads((base / ptr["json"]).read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError):
        return None
