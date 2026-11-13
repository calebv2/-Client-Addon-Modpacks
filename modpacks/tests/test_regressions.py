import hashlib
import importlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
engine = importlib.import_module("modpacks.engine")
source = importlib.import_module("modpacks.source")


def digest(data):
    return hashlib.sha256(data).hexdigest().upper()


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.game = self.temp.name
        self.mods = Path(self.game, "Mods")
        self.mods.mkdir()
        self.ledger = str(Path(self.game, "ledger.json"))
        self.pack = engine.create_pack(self.game, "Test", "all")
        self.url = "https://example.test/manifest.json"

    def manifest(self, files):
        return source.Manifest(self.url, [
            {"filename": name, "sha256": digest(data), "url": "https://example.test/" + name}
            for name, data in files.items()], "Test", [], [])

    def install(self, files):
        for name, data in files.items():
            Path(self.pack.path, name).write_bytes(data)
        self.pack.source = {"url": self.url, "files": {name.lower(): digest(data)
                            for name, data in files.items()},
                            "listed": {name.lower(): name for name in files}}
        self.pack.save()

    def fetcher(self, url, limit):
        return url, b"new"

    def test_failed_update_restores_every_file(self):
        self.install({"A.dll": b"old-a", "B.dll": b"old-b", "Removed.dll": b"removed"})
        original = Path(self.pack.path, "pack.json").read_bytes()
        replace = os.replace

        def fail_second(src, dst):
            if Path(src).parent.name == "Test" and "_downloads" in Path(src).parts and Path(src).name == "B.dll":
                raise PermissionError("locked file")
            return replace(src, dst)

        with patch.object(source.os, "replace", side_effect=fail_second):
            with self.assertRaises(source.SourceError):
                source.sync(self.game, self.pack, self.manifest({"A.dll": b"new", "B.dll": b"new"}), self.fetcher)
        for name, data in {"A.dll": b"old-a", "B.dll": b"old-b", "Removed.dll": b"removed"}.items():
            self.assertEqual(Path(self.pack.path, name).read_bytes(), data)
        self.assertEqual(Path(self.pack.path, "pack.json").read_bytes(), original)

    def test_failed_metadata_save_restores_update_and_deletion(self):
        self.install({"A.dll": b"old", "Removed.dll": b"removed"})
        original = self.pack.state()
        with patch.object(self.pack, "save", side_effect=PermissionError("locked metadata")):
            with self.assertRaises(source.SourceError):
                source.sync(self.game, self.pack, self.manifest({"A.dll": b"new", "New.dll": b"new"}), self.fetcher)
        self.assertEqual(Path(self.pack.path, "A.dll").read_bytes(), b"old")
        self.assertEqual(Path(self.pack.path, "Removed.dll").read_bytes(), b"removed")
        self.assertFalse(Path(self.pack.path, "New.dll").exists())
        self.assertEqual(self.pack.state(), original)

    def test_failed_rollback_preserves_recovery_files(self):
        self.install({"A.dll": b"old"})
        replace = os.replace

        def fail_restore(src, dst):
            if Path(src).parent.name == "_previous":
                raise PermissionError("locked restore")
            return replace(src, dst)

        with patch.object(self.pack, "save", side_effect=PermissionError("locked metadata")), \
                patch.object(source.os, "replace", side_effect=fail_restore):
            with self.assertRaisesRegex(source.SourceError, "Recovery files"):
                source.sync(self.game, self.pack, self.manifest({"A.dll": b"new"}), self.fetcher)
        saved = list(Path(self.game, "Modpacks", "_downloads").rglob("_previous/A.dll"))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].read_bytes(), b"old")

    def test_successful_update_and_removal(self):
        self.install({"A.dll": b"old", "Removed.dll": b"removed"})
        manifest = self.manifest({"A.dll": b"new", "New.dll": b"new"})
        source.sync(self.game, self.pack, manifest, self.fetcher)
        self.assertEqual(set(self.pack.files()), {"A.dll", "New.dll"})
        self.assertEqual(source.count(source.changes(engine.Pack(self.pack.path), manifest)), 0)

    def test_removed_mod_does_not_repeat_update_prompt(self):
        self.install({"A.dll": b"old"})
        Path(self.pack.path, "A.dll").unlink()
        self.pack.dropped = {"a.dll": digest(b"old")}
        manifest = self.manifest({"A.dll": b"new"})
        self.assertEqual(source.count(source.changes(self.pack, manifest)), 0)
        self.pack.source["listed"] = {}
        self.assertEqual(source.changes(self.pack, manifest)["dropped"], ["A.dll"])
        source.sync(self.game, self.pack, manifest, self.fetcher)
        self.assertEqual(source.count(source.changes(self.pack, manifest)), 0)
        self.assertFalse(Path(self.pack.path, "A.dll").exists())

    def test_bad_hash_changes_nothing(self):
        self.install({"A.dll": b"old"})
        with self.assertRaises(source.SourceError):
            source.sync(self.game, self.pack, self.manifest({"A.dll": b"expected"}), self.fetcher)
        self.assertEqual(Path(self.pack.path, "A.dll").read_bytes(), b"old")

    def test_edit_during_download_is_not_overwritten(self):
        self.install({"A.dll": b"old"})

        def edit_during_download(url, limit):
            Path(self.pack.path, "A.dll").write_bytes(b"my edit")
            return url, b"new"

        with self.assertRaisesRegex(source.SourceError, "pack changed"):
            source.sync(self.game, self.pack, self.manifest({"A.dll": b"new"}), edit_during_download)
        self.assertEqual(Path(self.pack.path, "A.dll").read_bytes(), b"my edit")

    def test_settings_changed_during_download_are_preserved(self):
        self.install({"A.dll": b"old"})

        def edit_during_download(url, limit):
            fresh = engine.Pack(self.pack.path)
            fresh.mode = "off"
            fresh.save()
            return url, b"new"

        with self.assertRaisesRegex(source.SourceError, "pack changed"):
            source.sync(self.game, self.pack, self.manifest({"A.dll": b"new"}), edit_during_download)
        self.assertEqual(engine.Pack(self.pack.path).mode, "off")
        self.assertEqual(Path(self.pack.path, "A.dll").read_bytes(), b"old")

    def test_removed_link_is_not_reinstated_by_stale_report(self):
        self.install({"A.dll": b"old"})
        manifest = self.manifest({"A.dll": b"new"})
        report = [(self.pack.name, manifest, source.changes(self.pack, manifest), None)]
        source.unlink(self.pack)
        self.assertEqual(source.update_all(self.game, report, self.fetcher), [])
        self.assertEqual(Path(self.pack.path, "A.dll").read_bytes(), b"old")

    def test_launch_rollback_keeps_loose_backup_and_managed_copy(self):
        self.install({"A.dll": b"managed"})
        (self.mods / "A.dll").write_bytes(b"loose")
        engine.apply(self.game, None, self.ledger, running=lambda _: False)
        saved = engine.read_shadowed(self.game)
        self.pack.mode = "off"
        self.pack.save()
        write_json = engine.write_json
        calls = 0

        def fail_final_ledger(path, data):
            nonlocal calls
            if str(path) == self.ledger:
                calls += 1
                if calls == 2:
                    raise PermissionError("locked ledger")
            return write_json(path, data)

        with patch.object(engine, "write_json", side_effect=fail_final_ledger):
            with self.assertRaises(engine.ModpacksError):
                engine.apply(self.game, None, self.ledger, running=lambda _: False)
        self.assertEqual((self.mods / "A.dll").read_bytes(), b"managed")
        self.assertEqual(engine.read_shadowed(self.game), saved)
        self.assertEqual(Path(engine.shadowed_dir(self.game), saved[0]["backup"]).read_bytes(), b"loose")
        engine.apply(self.game, None, self.ledger, running=lambda _: False)
        self.assertEqual((self.mods / "A.dll").read_bytes(), b"loose")

    def test_homepage_error_explains_manifest_link(self):
        with self.assertRaisesRegex(source.SourceError, "manifest.json"):
            source.read_manifest(self.url, lambda url, limit: (url, b"<!doctype html>"))


if __name__ == "__main__":
    unittest.main()
