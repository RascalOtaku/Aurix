#!/usr/bin/env python3
"""aurix_restore.py - list, verify and restore the snapshots made by scripts/aurix_backup.sh.

The nightly backup script ended its header with "Restore: copy what you need back from
<NAS>/aurix-backup/snapshots/<stamp>/". That works, but it is easy to get wrong in the one moment when it
matters: picking a half-written snapshot, restoring a corrupt SQLite file over a good one, or overwriting live
data with no copy of what was there. This helper makes the restore a reviewed, reversible step:

    python3 scripts/aurix_restore.py list                     what snapshots exist, with sizes and parts
    python3 scripts/aurix_restore.py verify latest            structure + SQLite integrity, read-only
    python3 scripts/aurix_restore.py restore latest --to /tmp/stage          preview (writes nothing)
    python3 scripts/aurix_restore.py restore latest --to /tmp/stage --apply  actually copy

Safety rules, all enforced in code (and covered by tests/test_aurix_restore.py):
  * A snapshot is opened READ-ONLY. Verification opens SQLite with mode=ro&immutable=1, so it never creates a
    -wal/-shm file inside the snapshot; nothing here ever writes into <root>/snapshots/.
  * `restore` verifies first and refuses a snapshot whose SQLite integrity check fails. A half-written or
    corrupt database is never put on top of live data by this tool.
  * Without --apply, `restore` only prints the plan. With --apply it never deletes anything in the destination:
    the restore is additive, because a snapshot is not a full mirror of the live tree.
  * Existing files are only replaced with --overwrite, and each file that would be lost is first copied to
    <destination>/.aurix-restore-pre/<stamp>/, so even an in-place restore can be undone by hand.
  * The default destination is a staging folder next to the snapshots (<root>/restore/<stamp>), so the safe
    command never touches live data. Point --brain-to / --app-to at the real folders only when you mean it.
  * Snapshots deliberately do not contain .env, auth.json, ssh/, *.pem, *.key or credentials* (see the backup
    script). Restore prints a reminder; secrets have to be put back from somewhere else.

Path defaults follow the backup script's environment: AURIX_BACKUP_ROOT (or AURIX_BACKUP_NAS/aurix-backup).
The live folders are only ever written when you name them with --brain-to / --app-to. Stdlib only.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Dict, Iterable, List, NamedTuple, Optional, Tuple
from urllib.parse import quote

SNAPSHOT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}$")
PARTS = ("brain", "app")
DEFAULT_NAS = "/mnt/hearthnode"


class RestoreError(Exception):
    """A refusal with a reason the owner can act on. Never swallowed into a traceback."""


class FileOp(NamedTuple):
    src: Path
    dst: Path
    rel: str          # path inside the snapshot, used for the pre-restore copy and reporting
    part: str


def backup_root(root: Optional[Path] = None) -> Path:
    """<NAS>/aurix-backup, the same folder aurix_backup.sh treats as its opt-in marker."""
    if root is not None:
        return Path(root)
    env = os.environ.get("AURIX_BACKUP_ROOT")
    if env:
        return Path(env).expanduser()
    nas = os.environ.get("AURIX_BACKUP_NAS", DEFAULT_NAS)
    return Path(nas).expanduser() / "aurix-backup"


def snapshots_dir(root: Optional[Path] = None) -> Path:
    return backup_root(root) / "snapshots"


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def list_snapshots(root: Optional[Path] = None) -> List[dict]:
    """Real snapshots only, newest first. `latest` (a symlink) and `.partial` runs are ignored."""
    base = snapshots_dir(root)
    if not base.is_dir():
        return []
    out: List[dict] = []
    for entry in base.iterdir():
        # is_symlink() first: `latest` points at a real stamp and must not be listed twice.
        if entry.is_symlink() or not entry.is_dir() or not SNAPSHOT_RE.match(entry.name):
            continue
        files = 0
        size = 0
        for dirpath, _dirnames, filenames in os.walk(entry):
            for name in filenames:
                p = Path(dirpath) / name
                try:
                    if p.is_symlink():
                        continue
                    size += p.stat().st_size
                    files += 1
                except OSError:
                    continue
        out.append({
            "name": entry.name,
            "path": entry,
            "files": files,
            "bytes": size,
            "parts": [p for p in PARTS if (entry / p).is_dir()],
            "databases": len(list(entry.rglob("*.db"))),
        })
    out.sort(key=lambda s: s["name"], reverse=True)
    return out


def resolve_snapshot(name: str, root: Optional[Path] = None) -> Path:
    """Turn a stamp (or `latest`) into a snapshot directory, refusing anything outside <root>/snapshots/."""
    base = snapshots_dir(root)
    if name == "latest":
        link = base / "latest"
        if not link.exists():
            raise RestoreError(f"no 'latest' marker in {base} (pass a stamp from `list`)")
        path = link.resolve()
        name = path.name
    if not SNAPSHOT_RE.match(name):
        raise RestoreError(f"not a snapshot name: {name!r} (expected YYYY-MM-DD_HHMM or 'latest')")
    path = (base / name).resolve()
    if not _inside(path, base) or path.parent != base.resolve():
        raise RestoreError(f"refusing a snapshot outside {base}: {path}")
    if not path.is_dir():
        raise RestoreError(f"no snapshot called {name} in {base}")
    return path


def _sqlite_check(db: Path) -> str:
    """SQLite's own integrity check, strictly read-only. Returns 'ok' or the failure text."""
    uri = f"file:{quote(str(db))}?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return f"cannot read database: {exc}"
    verdict = (row[0] if row else "") or "no result"
    return verdict


def verify_snapshot(path: Path) -> dict:
    """Structure and SQLite integrity of one snapshot. `ok` is False only for reasons that make a restore unsafe."""
    problems: List[str] = []
    warnings: List[str] = []
    databases: List[str] = []
    files = 0
    size = 0

    if not path.is_dir():
        return {"ok": False, "problems": [f"not a directory: {path}"], "warnings": [], "databases": [],
                "files": 0, "bytes": 0, "parts": []}

    for db in sorted(path.rglob("*.db")):
        verdict = _sqlite_check(db)
        rel = str(db.relative_to(path))
        if verdict == "ok":
            databases.append(rel)
        else:
            problems.append(f"SQLite integrity check failed for {rel}: {verdict}")

    parts = [p for p in PARTS if (path / p).is_dir()]
    if not parts:
        problems.append(f"snapshot has neither a brain/ nor an app/ folder: {path}")
    else:
        missing = [p for p in PARTS if p not in parts]
        if missing:
            warnings.append(f"no {' or '.join(missing)}/ folder in this snapshot")
        for part in parts:
            if not any((path / part).iterdir()):
                warnings.append(f"{part}/ is empty")

    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            p = Path(dirpath) / name
            try:
                if p.is_symlink():
                    continue
                size += p.stat().st_size
                files += 1
            except OSError:
                continue

    if files == 0:
        warnings.append("snapshot contains no files")

    return {"ok": not problems, "problems": problems, "warnings": warnings, "databases": databases,
            "files": files, "bytes": size, "parts": parts}


def build_plan(snapshot: Path, destinations: Dict[str, Path]) -> Tuple[List[FileOp], List[str]]:
    """What a restore would copy, and which of those destinations already exist (i.e. would be replaced)."""
    ops: List[FileOp] = []
    for part, dest in destinations.items():
        src_root = snapshot / part
        if not src_root.is_dir():
            continue
        for dirpath, _dirnames, filenames in os.walk(src_root):
            for name in filenames:
                src = Path(dirpath) / name
                rel = str(src.relative_to(src_root))
                ops.append(FileOp(src=src, dst=Path(dest) / rel, rel=rel, part=part))
    ops.sort(key=lambda o: (o.part, o.rel))
    conflicts = [str(o.dst) for o in ops if o.dst.exists() or o.dst.is_symlink()]
    return ops, conflicts


def _pre_restore_path(dest_root: Path, stamp: str, op: FileOp) -> Path:
    return dest_root / ".aurix-restore-pre" / stamp / op.part / op.rel


def _blocked_parent(op: FileOp, root: Path) -> Optional[Path]:
    """A parent path that exists as a file (or symlink to one) would make the copy fail halfway; find it first."""
    parent = op.dst.parent
    while parent != root and parent.parent != parent:
        if parent.exists() and not parent.is_dir():
            return parent
        parent = parent.parent
    return None


def apply_restore(ops: Iterable[FileOp], destinations: Dict[str, Path], stamp: str, overwrite: bool) -> dict:
    """Copy the plan into place. Refuses (before writing anything) if a destination exists and overwrite is off."""
    ops = list(ops)
    blocked = []
    for op in ops:
        blocker = _blocked_parent(op, destinations[op.part])
        if blocker:
            blocked.append(str(blocker))
    if blocked:
        raise RestoreError(
            f"{len(blocked)} destination path(s) exist as files, nothing was written: {', '.join(blocked[:3])}. "
            "Move them aside first; a restore never deletes or replaces a folder with a file."
        )
    conflicts = [o for o in ops if o.dst.exists() or o.dst.is_symlink()]
    if conflicts and not overwrite:
        sample = ", ".join(str(o.dst) for o in conflicts[:3])
        more = "" if len(conflicts) <= 3 else f" (+{len(conflicts) - 3} more)"
        raise RestoreError(
            f"{len(conflicts)} destination file(s) already exist, nothing was written: {sample}{more}. "
            f"Re-run with --overwrite to replace them (each replaced file is copied to "
            f"<destination>/.aurix-restore-pre/{stamp}/ first), or choose an empty --to folder."
        )

    copied = 0
    saved = 0
    for op in ops:
        op.dst.parent.mkdir(parents=True, exist_ok=True)
        if op.dst.exists() or op.dst.is_symlink():
            safe = _pre_restore_path(destinations[op.part], stamp, op)
            safe.parent.mkdir(parents=True, exist_ok=True)
            if op.dst.is_symlink():
                # Preserve the old link text, then remove it so copy2 can write a real file.
                try:
                    safe.write_text(os.readlink(op.dst), encoding="utf-8")
                except OSError:
                    pass
                op.dst.unlink()
            else:
                shutil.copy2(op.dst, safe)
            saved += 1
        if op.src.is_symlink():
            target = os.readlink(op.src)
            if op.dst.exists() or op.dst.is_symlink():
                op.dst.unlink()
            os.symlink(target, op.dst)
        else:
            tmp = op.dst.with_name(op.dst.name + ".aurix-restore-tmp")
            shutil.copy2(op.src, tmp)
            os.replace(tmp, op.dst)
        copied += 1
    return {"copied": copied, "replaced": saved}


def human_size(n: int) -> str:
    value = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if value < 1024 or unit == "T":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{n}B"


def _cmd_list(args) -> int:
    snapshots = list_snapshots(args.root)
    if not snapshots:
        print(f"no snapshots in {snapshots_dir(args.root)}")
        print("Backups are opt-in: create the marker folder on the NAS (<NAS>/aurix-backup), "
              "then run scripts/aurix_backup.sh.")
        return 1
    header = f"{'SNAPSHOT':20}  {'SIZE':>8}  {'FILES':>7}  {'DB':>3}  {'PARTS':<10}"
    if args.verify:
        header += "  VERIFIED"
    print(header)
    for snap in snapshots:
        line = (f"{snap['name']:20}  {human_size(snap['bytes']):>8}  {snap['files']:>7}  {snap['databases']:>3}  "
                f"{','.join(snap['parts']) or '-':<10}")
        if args.verify:
            line += "  " + ("ok" if verify_snapshot(snap["path"])["ok"] else "FAILED")
        print(line)
    link = snapshots_dir(args.root) / "latest"
    if link.is_symlink():
        print(f"latest -> {os.readlink(link)}")
    return 0


def _cmd_verify(args) -> int:
    path = resolve_snapshot(args.snapshot, args.root)
    result = verify_snapshot(path)
    print(f"snapshot: {path}")
    print(f"  parts:     {', '.join(result['parts']) or '-'}")
    print(f"  files:     {result['files']} ({human_size(result['bytes'])})")
    print(f"  databases: {', '.join(result['databases']) or '-'}")
    for warning in result["warnings"]:
        print(f"  warning:   {warning}")
    for problem in result["problems"]:
        print(f"  PROBLEM:   {problem}")
    print("  result:    " + ("OK - safe to restore" if result["ok"] else "FAILED - do not restore this snapshot"))
    return 0 if result["ok"] else 1


def _destinations(args, snapshot: Path) -> Dict[str, Path]:
    """Where each part goes. Default is a staging folder next to the snapshots, never live data."""
    if (args.brain_to or args.app_to) and args.to:
        raise RestoreError("use either --to (staging) or --brain-to/--app-to (explicit folders), not both")
    if args.brain_to or args.app_to:
        chosen: Dict[str, Path] = {}
        if args.brain_to:
            chosen["brain"] = Path(args.brain_to).expanduser()
        if args.app_to:
            chosen["app"] = Path(args.app_to).expanduser()
    else:
        base = Path(args.to).expanduser() if args.to else backup_root(args.root) / "restore" / snapshot.name
        chosen = {part: base / part for part in PARTS if (snapshot / part).is_dir()}
    for part, dest in chosen.items():
        if not (snapshot / part).is_dir():
            raise RestoreError(f"snapshot has no {part}/ folder, so there is nothing to restore into {dest}")
        if dest.exists() and not dest.is_dir():
            raise RestoreError(f"destination exists and is not a folder: {dest}")
        if _inside(dest, snapshots_dir(args.root)):
            raise RestoreError(f"refusing to write inside the snapshot tree: {dest}")
    return chosen


def _cmd_restore(args) -> int:
    path = resolve_snapshot(args.snapshot, args.root)
    result = verify_snapshot(path)
    if not result["ok"]:
        for problem in result["problems"]:
            print(f"refusing: {problem}", file=sys.stderr)
        print("Nothing was written. Pick another snapshot (`list`) or investigate the NAS copy first.", file=sys.stderr)
        return 1
    for warning in result["warnings"]:
        print(f"note: {warning}")

    destinations = _destinations(args, path)
    ops, conflicts = build_plan(path, destinations)
    print(f"snapshot: {path.name}")
    for part, dest in destinations.items():
        count = sum(1 for o in ops if o.part == part)
        print(f"  {part:5} -> {dest}  ({count} file(s))")
    if conflicts:
        print(f"  {len(conflicts)} destination file(s) already exist")
        if not args.overwrite:
            print("  they would be replaced only with --overwrite (each replaced file is saved first)")

    if not args.apply:
        print("dry run: nothing was written. Add --apply to perform this restore.")
        return 0

    stats = apply_restore(ops, destinations, path.name, overwrite=args.overwrite)
    print(f"restored: {stats['copied']} file(s) copied, {stats['replaced']} replaced (pre-restore copies kept).")
    damaged = 0
    for dest in destinations.values():
        for db in sorted(Path(dest).rglob("*.db")):
            verdict = _sqlite_check(db)
            print(f"  integrity {db.name}: {verdict}")
            if verdict != "ok":
                damaged += 1
    if damaged:
        print(f"{damaged} restored database(s) did not pass integrity_check. Do not start Aurix on this copy; "
              f"the snapshot passed verification, so suspect the destination disk or a concurrent writer.",
              file=sys.stderr)
        return 1
    print("Secrets are not in snapshots: put .env / auth.json / ssh keys back from their own copy.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aurix_restore.py",
        description="List, verify and restore the snapshots made by scripts/aurix_backup.sh (stdlib only).",
    )
    parser.add_argument("--root", type=Path, default=None,
                        help="backup root (default: $AURIX_BACKUP_ROOT or $AURIX_BACKUP_NAS/aurix-backup)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="show snapshots, newest first")
    p_list.add_argument("--verify", action="store_true", help="also run the SQLite integrity check on each snapshot")
    p_list.set_defaults(func=_cmd_list)

    p_verify = sub.add_parser("verify", help="check one snapshot's structure and SQLite integrity (read-only)")
    p_verify.add_argument("snapshot", help="a stamp from `list`, or 'latest'")
    p_verify.set_defaults(func=_cmd_verify)

    p_restore = sub.add_parser("restore", help="copy a verified snapshot into a folder (preview unless --apply)")
    p_restore.add_argument("snapshot", help="a stamp from `list`, or 'latest'")
    p_restore.add_argument("--to", help="staging folder: files go to <DIR>/brain and <DIR>/app")
    p_restore.add_argument("--brain-to", help="restore the brain part straight into this folder")
    p_restore.add_argument("--app-to", help="restore the app data part straight into this folder")
    p_restore.add_argument("--apply", action="store_true", help="actually copy (default is a dry-run preview)")
    p_restore.add_argument("--overwrite", action="store_true",
                           help="replace existing destination files, keeping a copy of each in .aurix-restore-pre/")
    p_restore.set_defaults(func=_cmd_restore)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except RestoreError as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        # Disk full, permission denied, a folder where a file should go: say what failed, keep the traceback out.
        print(f"failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
