import hashlib
import importlib
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
engine = importlib.import_module("modpacks.engine")
source = importlib.import_module("modpacks.source")

# The launcher ships these modules inside its executable. Use small stand-ins
# so addon tests neither start the launcher nor patch the real subprocess API.
shared = ModuleType("tavern_shared")
shared.__path__ = []
theme = ModuleType("tavern_shared.theme")
theme._btn = Mock()
theme.BG = "#202020"
theme.PARCH = "#eeeeee"
chrome = ModuleType("tavern_shared.window_chrome")
with patch.dict(sys.modules, {"tavern_shared": shared, "tavern_shared.theme": theme,
                             "tavern_shared.window_chrome": chrome}), \
        patch.object(subprocess.Popen, "_modpacks", True, create=True):
    client = importlib.import_module("modpacks.client")
window = client.window


@contextmanager
def dialog_style():
    """Provide real Tk controls for UI checks outside the packaged launcher."""
    colors = dict(BG="#171513", SURF="#211e1a", BORDER="#40392f", PARCH="#eee3cf",
                  MUTED="#ada496", AMBER="#e5b96f", GREEN="#9ac59a", RED="#e49a8d")

    def button(parent, text, cmd, style="normal", **kwargs):
        return tk.Button(parent, text=text, command=cmd, relief="flat", bd=0,
                         bg=colors["AMBER"] if style == "primary" else colors["SURF"],
                         fg=colors["BG"] if style == "primary" else colors["PARCH"], **kwargs)

    colors.update(_btn=button, _mk_scrollbar=lambda parent, command: tk.Scrollbar(parent, command=command))
    with patch.dict(theme.__dict__, colors), patch.dict(chrome.__dict__, {
            "_start_hidden": lambda widget: widget.withdraw(),
            "_finish_dark_window": lambda widget: widget.deiconify()}):
        yield


def sample_update_packs():
    return [{"name": "Sheikah Sanctuary", "host": "att.example.test", "sections": [
        {"key": "updated", "title": "To update", "color": "AMBER", "badge": "UPDATE", "rows": [
            {"name": "Dyeing API", "detail": "DyeingAPI.dll  ·  Installed 1.0.0"},
            {"name": "Tavern Essentials", "detail": "TavernEssentials.dll  ·  Installed 2.4.1"}]},
        {"key": "new", "title": "New mods", "color": "GREEN", "badge": "NEW", "rows": [
            {"name": "Night Skeletons", "detail": "NightSkeletons.dll"}]},
        {"key": "removed", "title": "To remove", "color": "RED", "badge": "REMOVE", "rows": [
            {"name": "Old Weather", "detail": "OldWeather.dll\nThe unchanged linked copy will be removed from this pack."}]},
        {"key": "kept", "title": "Staying as they are", "color": "MUTED", "badge": "KEEP", "rows": [
            {"name": "My Custom Mod", "detail": "MyCustomMod.dll\nYou changed this mod, so your local copy will be kept."}]}]},
        {"name": "Shared quality of life", "host": "mods.example.test", "sections": [
            {"key": "updated", "title": "To update", "color": "AMBER", "badge": "UPDATE", "rows": [
                {"name": "Inventory Helpers", "detail": "InventoryHelpers.dll  ·  Installed 1.8.0"}]}]}]


class UpdateFlowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.game = temp.name
        Path(self.game, "Mods").mkdir()
        self.pack = engine.create_pack(self.game, "Test", "all")
        self.url = "https://example.test/manifest.json"
        self.pack.source = {"url": self.url, "files": {}}
        self.pack.save()
        self.manifest = self.make_manifest(b"new")
        self.root = SimpleNamespace(_modpacks_pending=True, _modpacks_button=Mock())
        for patcher in (patch.object(source, "manifest_checks", source.ManifestChecks()),
                        patch.object(source, "_deferred", {}),
                        patch.object(client, "_app", return_value=self.root),
                        patch.object(client, "_log")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_manifest(self, data):
        return source.Manifest(self.url, [{"filename": "A.dll", "url": "https://example.test/A.dll",
                                          "sha256": hashlib.sha256(data).hexdigest().upper()}], "Test", [], [])

    def report(self):
        return [(self.pack.name, self.manifest, source.changes(self.pack, self.manifest), None)]

    def test_startup_and_join_share_recent_manifest_but_recheck_disk(self):
        with patch.object(source, "read_manifest", return_value=self.manifest) as read:
            self.assertEqual(source.count(source.check_all(self.game)[0][2]), 1)
            source.sync(self.game, self.pack, self.manifest, lambda url, limit: (url, b"new"))
            self.assertEqual(source.count(source.check_target(self.game, ("server.test", 1757))[0][2]), 0)
        read.assert_called_once()

    def test_concurrent_checks_share_inflight_request(self):
        entered, release = threading.Event(), threading.Event()

        def read(*args):
            entered.set()
            self.assertTrue(release.wait(3))
            return self.manifest

        with patch.object(source, "read_manifest", side_effect=read) as request:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(source.manifest_checks.get, self.url)
                try:
                    self.assertTrue(entered.wait(3))
                    second = pool.submit(source.manifest_checks.get, self.url)
                finally:
                    release.set()
                self.assertIs(first.result(3), second.result(3))
        request.assert_called_once()

    def test_expired_check_and_manual_check_fetch_again(self):
        with patch.object(source.time, "monotonic", return_value=100) as clock, \
                patch.object(source, "read_manifest", return_value=self.manifest) as read:
            source.manifest_checks.get(self.url)
            clock.return_value = 219
            source.manifest_checks.get(self.url)
            self.assertEqual(read.call_count, 1)
            clock.return_value = 221
            source.manifest_checks.get(self.url)
            source.manifest_checks.get(self.url, force=True)
            self.assertEqual(read.call_count, 3)

    def test_failed_check_does_not_repeat_immediately_but_retries(self):
        with patch.object(source.time, "monotonic", return_value=100) as clock, \
                patch.object(source, "read_manifest", side_effect=source.SourceError("offline")) as read:
            self.assertIn("offline", source.check_all(self.game)[0][3])
            source.check_target(self.game, ("server.test", 1757))
            self.assertEqual(read.call_count, 1)
            clock.return_value = 111
            source.check_target(self.game, ("server.test", 1757))
            self.assertEqual(read.call_count, 2)

    def test_declining_startup_does_not_prompt_again_on_join(self):
        report = self.report()
        with patch.object(window, "ask_updates", return_value=False) as ask, \
                patch.object(window, "run_waiting", side_effect=lambda root, work, text: work()), \
                patch.object(source, "read_manifest", return_value=self.manifest):
            client._checked(self.root, self.game, report, None)
            client._check_join_updates(self.game, ("server.test", 1757))
        ask.assert_called_once()
        self.assertFalse(self.root._modpacks_pending)

    def test_new_changes_prompt_after_previous_decline(self):
        source.defer_changes(self.pack, self.manifest, source.changes(self.pack, self.manifest))
        newer = self.make_manifest(b"newer")
        with patch.object(window, "ask_updates", return_value=False) as ask, \
                patch.object(window, "run_waiting", side_effect=lambda root, work, text: work()), \
                patch.object(source, "read_manifest", return_value=newer):
            client._check_join_updates(self.game, ("server.test", 1757))
        ask.assert_called_once()

    def test_stale_startup_report_does_not_prompt_after_update(self):
        report = self.report()
        source.sync(self.game, self.pack, self.manifest, lambda url, limit: (url, b"new"))
        with patch.object(window, "ask_updates") as ask:
            client._checked(self.root, self.game, report, None)
        ask.assert_not_called()
        self.assertFalse(self.root._modpacks_pending)

    def test_join_waits_for_accepted_startup_update(self):
        queued = []
        with patch.object(window, "ask_updates", return_value=True), \
                patch.object(window, "background", side_effect=lambda root, work, done: queued.append((work, done))):
            client._checked(self.root, self.game, self.report(), None)
        self.assertTrue(self.root._modpacks_pending)

        def wait(root, ready, text):
            self.assertFalse(ready())
            work, done = queued.pop()
            done(work(), None)
            self.assertTrue(ready())

        def launch(args):
            self.assertFalse(self.root._modpacks_pending)
            self.assertEqual(Path(self.pack.path, "A.dll").read_bytes(), b"new")

        with patch.object(window, "wait_for", side_effect=wait), \
                patch.object(source, "fetch", side_effect=lambda url, limit: (url, b"new")), \
                patch.object(engine, "game_running", return_value=False), \
                patch.object(client, "_before_launch", side_effect=launch) as launch_hook:
            client.before_launch([str(Path(self.game, engine.GAME_EXE))])
        launch_hook.assert_called_once()
        self.assertFalse(self.root._modpacks_launching)

    def test_already_running_game_skips_update_prompts(self):
        with patch.object(engine, "game_running", return_value=True), \
                patch.object(client, "_before_launch") as launch:
            client.before_launch([str(Path(self.game, engine.GAME_EXE))])
        launch.assert_not_called()

    def test_failed_startup_check_releases_launch(self):
        client._checked(self.root, self.game, None, source.SourceError("offline"))
        self.assertFalse(self.root._modpacks_pending)

    def test_status_text_does_not_create_duplicate_modpacks_button(self):
        button = self.root._modpacks_button
        button.cget.return_value = client.BUTTON_TEXT + " · Checking..."
        self.root.v_exe = Mock()
        anchor = Mock()
        anchor.winfo_toplevel.return_value = self.root
        anchor.master.pack_slaves.return_value = [anchor, button]
        anchor.master.winfo_children.return_value = [anchor, button]
        with patch.object(client._theme, "_btn") as create:
            client._add_button(anchor)
        create.assert_not_called()

    def test_update_sections_distinguish_removals_from_protected_files(self):
        found = {"new": [], "updated": [], "removed": ["Old.dll"],
                 "unlisted": ["Old.dll", "CircuitsVoiceChat.dll", "Gone.dll"],
                 "protected": ["CircuitsVoiceChat.dll"], "kept": [], "dropped": ["Skipped.dll"]}
        sections = window.update_sections(self.pack, found)
        removal = next(section for section in sections if section["key"] == "removed")
        kept = next(section for section in sections if section["key"] == "kept")
        self.assertEqual([row["name"] for row in removal["rows"]], ["Old"])
        self.assertEqual(len(kept["rows"]), 3)
        details = "\n".join(row["detail"] for row in kept["rows"])
        self.assertIn("Managed by the launcher; left unchanged", details)
        self.assertIn("No installed file will be deleted", details)
        self.assertIn("will not be downloaded", details)

    def test_update_sections_show_installed_versions_and_sort_names(self):
        found = {"new": [], "updated": [{"filename": "Z.dll"}, {"filename": "A.dll"}],
                 "removed": [], "kept": []}
        with patch.object(engine, "mod_info", return_value={"name": "Friendly mod", "version": "1.2.3"}):
            rows = window.update_sections(self.pack, found)[0]["rows"]
        self.assertEqual(rows[0]["name"], "Friendly mod")
        self.assertIn("Installed 1.2.3", rows[0]["detail"])
        with patch.object(engine, "mod_info", return_value=None):
            rows = window.update_sections(self.pack, found)[0]["rows"]
        self.assertEqual([row["name"] for row in rows], ["A", "Z"])


class UpdateDialogTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        style = dialog_style()
        style.__enter__()
        self.addCleanup(style.__exit__, None, None, None)

    def test_long_list_scrolls_while_actions_stay_visible(self):
        packs = sample_update_packs()
        packs[0]["sections"][0]["rows"] *= 20
        packs[0]["name"] = "A very long server pack name that must fit without hiding the mod actions"
        dialog = window.UpdateDialog(self.root, packs, server="server.example.test:1757")
        self.addCleanup(dialog.destroy)
        dialog.geometry("640x540")
        dialog.update()
        self.assertTrue(dialog.apply_button.winfo_viewable())
        self.assertTrue(dialog.skip_button.winfo_viewable())
        self.assertGreater(dialog.list.inner.winfo_reqheight(), dialog.list.canvas.winfo_height())
        self.assertLessEqual(dialog.apply_button.winfo_rootx() + dialog.apply_button.winfo_width(),
                             dialog.winfo_rootx() + dialog.winfo_width())
        self.assertEqual(dialog.apply_button.cget("text"), "Apply & join")
        self.assertEqual(dialog.counts["removed"], 1)
        dialog.list.canvas.yview_moveto(1)
        dialog.update()
        self.assertGreater(dialog.list.canvas.yview()[0], 0)

    def test_close_and_apply_return_distinct_choices(self):
        dialog = window.UpdateDialog(self.root, sample_update_packs())
        self.root.tk.call(dialog.protocol("WM_DELETE_WINDOW"))
        self.assertFalse(dialog.choice)
        dialog = window.UpdateDialog(self.root, sample_update_packs())
        dialog.apply_button.invoke()
        self.assertTrue(dialog.choice)


class WaitingWindowTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def test_worker_wait_keeps_tk_responsive(self):
        ready = threading.Event()
        self.root.after(50, ready.set)
        value = window.run_waiting(self.root, lambda: ready.wait(3), "Checking...")
        self.assertTrue(value)
        self.assertFalse(self.root._modpacks_pending)
        self.assertIsNone(self.root._modpacks_wait_dialog)

    def test_destroyed_wait_dialog_cancels_the_launch(self):
        self.root.after(80, lambda: self.root._modpacks_wait_dialog.destroy())
        with self.assertRaisesRegex(engine.ModpacksError, "Launch cancelled"):
            window.wait_for(self.root, lambda: False, "Checking...")

    def test_cancelled_wait_keeps_update_pending_until_worker_finishes(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def cancel():
            dialog = self.root._modpacks_wait_dialog
            self.root.tk.call(dialog.protocol("WM_DELETE_WINDOW"))

        self.root.after(80, cancel)
        with self.assertRaisesRegex(engine.ModpacksError, "Launch cancelled"):
            window.run_waiting(self.root, lambda: release.wait(3), "Updating...")
        self.assertTrue(self.root._modpacks_pending)
        release.set()
        window.wait_for(self.root, lambda: not self.root._modpacks_pending, "Finishing...")
        self.assertFalse(self.root._modpacks_pending)


if __name__ == "__main__":
    unittest.main()
