"""scripts/aurix_restore.py: the restore helper must be read-only on snapshots, refuse corrupt databases, and
never destroy live data. Every test builds its own fake NAS tree in a temp folder; the real backup is never touched."""
import contextlib
import importlib.util
import io
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "aurix_restore.py"
_spec = importlib.util.spec_from_file_location("aurix_restore", SCRIPT)
aurix_restore = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(aurix_restore)


def make_db(path: Path, rows: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE t (i INTEGER)")
        conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
        conn.commit()
    finally:
        conn.close()


def snapshot_files(path: Path):
    return sorted(str(p.relative_to(path)) for p in path.rglob("*"))


class RestoreHelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aurix_restore_test_"))
        self.root = self.tmp / "aurix-backup"
        self.snaps = self.root / "snapshots"
        self.stamp = "2026-10-01_0300"
        self.snap = self.snaps / self.stamp
        (self.snap / "brain" / "memory").mkdir(parents=True)
        (self.snap / "app" / "sub").mkdir(parents=True)
        (self.snap / "brain" / "memory" / "note.md").write_text("remembered", encoding="utf-8")
        (self.snap / "app" / "keep.txt").write_text("from snapshot", encoding="utf-8")
        (self.snap / "app" / "sub" / "nested.json").write_text("{}", encoding="utf-8")
        make_db(self.snap / "app" / "live.db", rows=7)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- list / resolve -------------------------------------------------------------------------------------

    def test_list_finds_only_real_snapshots_newest_first(self):
        older = self.snaps / "2026-09-01_0300"
        (older / "app").mkdir(parents=True)
        (self.snaps / "latest").symlink_to(self.stamp)
        (self.snaps / "important_notes").mkdir()
        (self.snaps / "2026-10-02_0300.partial").mkdir()
        names = [s["name"] for s in aurix_restore.list_snapshots(self.root)]
        self.assertEqual(names, ["2026-10-01_0300", "2026-09-01_0300"])
        listed = aurix_restore.list_snapshots(self.root)[0]
        self.assertEqual(listed["parts"], ["brain", "app"])
        self.assertEqual(listed["databases"], 1)
        self.assertEqual(listed["files"], 4)

    def test_list_is_empty_without_a_snapshots_folder(self):
        self.assertEqual(aurix_restore.list_snapshots(self.tmp / "nothing-here"), [])

    def test_resolve_accepts_a_stamp_and_latest(self):
        self.assertEqual(aurix_restore.resolve_snapshot(self.stamp, self.root), self.snap.resolve())
        (self.snaps / "latest").symlink_to(self.stamp)
        self.assertEqual(aurix_restore.resolve_snapshot("latest", self.root), self.snap.resolve())

    def test_resolve_refuses_traversal_unknown_names_and_missing_latest(self):
        for bad in ("../outside", "..", "/etc", "2026-10-01", "not-a-stamp", ""):
            with self.assertRaises(aurix_restore.RestoreError, msg=bad):
                aurix_restore.resolve_snapshot(bad, self.root)
        with self.assertRaises(aurix_restore.RestoreError):
            aurix_restore.resolve_snapshot("2026-10-09_0300", self.root)
        with self.assertRaises(aurix_restore.RestoreError):
            aurix_restore.resolve_snapshot("latest", self.root)

    # ---- verify ---------------------------------------------------------------------------------------------

    def test_verify_passes_a_good_snapshot_and_opens_it_read_only(self):
        before = snapshot_files(self.snap)
        result = aurix_restore.verify_snapshot(self.snap)
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["parts"], ["brain", "app"])
        self.assertIn("app/live.db", result["databases"])
        self.assertEqual(result["files"], 4)
        self.assertGreater(result["bytes"], 0)
        self.assertEqual(snapshot_files(self.snap), before, "verification must not create -wal/-shm or anything else")

    def test_verify_blocks_on_a_corrupt_database(self):
        (self.snap / "app" / "broken.db").write_bytes(b"this is not a sqlite file")
        result = aurix_restore.verify_snapshot(self.snap)
        self.assertFalse(result["ok"])
        self.assertTrue(any("broken.db" in p for p in result["problems"]), result["problems"])

    def test_verify_blocks_a_snapshot_with_no_parts(self):
        empty = self.snaps / "2026-09-30_0300"
        empty.mkdir()
        result = aurix_restore.verify_snapshot(empty)
        self.assertFalse(result["ok"])

    def test_verify_warns_but_allows_a_snapshot_with_only_one_part(self):
        import shutil
        shutil.rmtree(self.snap / "brain")
        result = aurix_restore.verify_snapshot(self.snap)
        self.assertTrue(result["ok"])
        self.assertTrue(any("brain" in w for w in result["warnings"]), result["warnings"])

    # ---- restore --------------------------------------------------------------------------------------------

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = aurix_restore.main(["--root", str(self.root)] + argv)
        return code, out.getvalue(), err.getvalue()

    def test_restore_preview_writes_nothing(self):
        stage = self.tmp / "stage"
        code, out, _err = self.run_cli(["restore", self.stamp, "--to", str(stage)])
        self.assertEqual(code, 0)
        self.assertIn("dry run", out)
        self.assertFalse(stage.exists(), "a preview must not create the destination")

    def test_restore_copies_both_parts_into_a_staging_folder(self):
        stage = self.tmp / "stage"
        code, out, _err = self.run_cli(["restore", self.stamp, "--to", str(stage), "--apply"])
        self.assertEqual(code, 0, out)
        self.assertEqual((stage / "brain" / "memory" / "note.md").read_text(encoding="utf-8"), "remembered")
        self.assertEqual((stage / "app" / "keep.txt").read_text(encoding="utf-8"), "from snapshot")
        self.assertEqual((stage / "app" / "sub" / "nested.json").read_text(encoding="utf-8"), "{}")
        conn = sqlite3.connect(stage / "app" / "live.db")
        try:
            self.assertEqual(conn.execute("select count(*) from t").fetchone()[0], 7)
        finally:
            conn.close()
        self.assertIn("Secrets are not in snapshots", out)
        self.assertEqual([p.name for p in stage.rglob("*.aurix-restore-tmp")], [])

    def test_restore_can_pick_a_single_part(self):
        app_dest = self.tmp / "live-app"
        code, _out, _err = self.run_cli(["restore", self.stamp, "--app-to", str(app_dest), "--apply"])
        self.assertEqual(code, 0)
        self.assertTrue((app_dest / "keep.txt").is_file())
        self.assertFalse((self.tmp / "brain").exists())

    def test_restore_refuses_to_replace_existing_files_without_overwrite(self):
        dest = self.tmp / "live-app"
        dest.mkdir()
        (dest / "keep.txt").write_text("live version", encoding="utf-8")
        code, _out, err = self.run_cli(["restore", self.stamp, "--app-to", str(dest), "--apply"])
        self.assertEqual(code, 1)
        self.assertIn("already exist", err)
        self.assertEqual((dest / "keep.txt").read_text(encoding="utf-8"), "live version")
        self.assertFalse((dest / "sub" / "nested.json").exists(), "a refused restore must not write anything")

    def test_overwrite_keeps_a_copy_of_every_replaced_file_and_deletes_nothing(self):
        dest = self.tmp / "live-app"
        dest.mkdir()
        (dest / "keep.txt").write_text("live version", encoding="utf-8")
        (dest / "unrelated.txt").write_text("not in the snapshot", encoding="utf-8")
        code, out, _err = self.run_cli(["restore", self.stamp, "--app-to", str(dest), "--apply", "--overwrite"])
        self.assertEqual(code, 0, out)
        self.assertEqual((dest / "keep.txt").read_text(encoding="utf-8"), "from snapshot")
        self.assertEqual((dest / "unrelated.txt").read_text(encoding="utf-8"), "not in the snapshot")
        saved = dest / ".aurix-restore-pre" / self.stamp / "app" / "keep.txt"
        self.assertEqual(saved.read_text(encoding="utf-8"), "live version")
        self.assertIn("1 replaced", out)

    def test_restore_refuses_a_corrupt_snapshot_and_writes_nothing(self):
        (self.snap / "app" / "broken.db").write_bytes(b"not sqlite")
        dest = self.tmp / "live-app"
        code, _out, err = self.run_cli(["restore", self.stamp, "--app-to", str(dest), "--apply"])
        self.assertEqual(code, 1)
        self.assertIn("broken.db", err)
        self.assertFalse(dest.exists())

    def test_restore_refuses_to_write_inside_the_snapshot_tree(self):
        code, _out, err = self.run_cli(["restore", self.stamp, "--app-to", str(self.snaps / "copy"), "--apply"])
        self.assertEqual(code, 1)
        self.assertIn("snapshot tree", err)
        self.assertFalse((self.snaps / "copy").exists())

    def test_default_destination_is_staging_next_to_the_snapshots(self):
        code, out, _err = self.run_cli(["restore", self.stamp, "--apply"])
        self.assertEqual(code, 0, out)
        stage = self.root / "restore" / self.stamp
        self.assertTrue((stage / "app" / "keep.txt").is_file())
        self.assertTrue((stage / "brain" / "memory" / "note.md").is_file())

    def test_restore_keeps_snapshot_symlinks_as_symlinks(self):
        (self.snap / "app" / "link.txt").symlink_to("keep.txt")
        stage = self.tmp / "stage"
        code, _out, _err = self.run_cli(["restore", self.stamp, "--to", str(stage), "--apply"])
        self.assertEqual(code, 0)
        self.assertTrue((stage / "app" / "link.txt").is_symlink())
        self.assertEqual(os.readlink(stage / "app" / "link.txt"), "keep.txt")

    def test_list_command_reports_snapshots_and_missing_backups(self):
        code, out, _err = self.run_cli(["list"])
        self.assertEqual(code, 0)
        self.assertIn(self.stamp, out)
        code, out, _err = self.run_cli(["list", "--verify"])
        self.assertIn("ok", out)
        self.assertEqual(aurix_restore.main(["--root", str(self.tmp / "empty"), "list"]), 1)

    def test_a_blocked_destination_fails_cleanly_and_writes_nothing(self):
        dest = self.tmp / "live-app"
        dest.mkdir()
        (dest / "sub").write_text("this is a file, not a folder", encoding="utf-8")
        code, _out, err = self.run_cli(["restore", self.stamp, "--app-to", str(dest), "--apply", "--overwrite"])
        self.assertEqual(code, 1)
        self.assertIn("exist as files", err)
        self.assertFalse((dest / "keep.txt").exists(), "a blocked restore must not write anything")

    def test_a_damaged_destination_copy_is_reported_as_a_failure(self):
        stage = self.tmp / "stage"
        real_check = aurix_restore._sqlite_check
        calls = {"n": 0}

        def flaky(db):
            # The snapshot check (verify) passes; the check of the freshly written copy does not.
            calls["n"] += 1
            return real_check(db) if calls["n"] == 1 else "database disk image is malformed"

        with mock.patch.object(aurix_restore, "_sqlite_check", side_effect=flaky):
            code, out, err = self.run_cli(["restore", self.stamp, "--to", str(stage), "--apply"])
        self.assertEqual(code, 1)
        self.assertIn("malformed", out + err)
        self.assertIn("did not pass integrity_check", err)
        self.assertNotIn("Secrets are not in snapshots", out)   # the happy-path advice would be misleading

    def test_verify_command_exit_code_follows_the_integrity_check(self):
        self.assertEqual(self.run_cli(["verify", self.stamp])[0], 0)
        (self.snap / "app" / "broken.db").write_bytes(b"not sqlite")
        self.assertEqual(self.run_cli(["verify", self.stamp])[0], 1)


if __name__ == "__main__":
    unittest.main()
