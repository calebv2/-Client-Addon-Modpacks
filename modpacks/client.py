import json
import os
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox

import tavern_shared.theme as _theme

from . import engine, source, window

CHECK_DELAY_MS = 3000
MODS_BUTTON = "\U0001F9EA Mods"
AFTER_TEXTS = ("\U0001F4C1 Mods Folder", "☑ Toggle Mods")
BUTTON_TEXT = "\U0001F4E6 Modpacks"
PREFERENCES_FILE = "modpacks_preferences.json"


def _app():
    return getattr(tk, "_default_root", None)


def _log(message, tag=""):
    app = _app()
    try:
        app._print(message, tag)
    except Exception:
        pass


def _alert(title, text):
    if threading.current_thread() is not threading.main_thread():
        return
    try:
        messagebox.showerror(title, text, parent=_app())
    except Exception:
        pass


def _is_game(args):
    if isinstance(args, (str, bytes)) or not args:
        return False
    try:
        first = os.fspath(args[0])
    except TypeError:
        return False
    if isinstance(first, bytes):
        return False
    if os.path.basename(first).lower() == engine.GAME_EXE.lower():
        return True
    try:
        exe = _app().v_exe.get().strip()
    except Exception:
        return False
    return bool(exe) and engine.same_path(first, exe)


def ask_conflict(conflicts, loose):
    dialog = window.ConflictDialog(_app(), conflicts, loose)
    dialog.wait_window()
    return dialog.choice


def _server_key(target):
    host, port = engine.server_key(target[0], target[1])
    return "%s:%d" % (host, port)


def _server_preferences():
    path = os.path.join(engine.data_dir(), PREFERENCES_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        _log("Modpacks: couldn't read saved server choices (%s); you'll be asked again"
             % (getattr(error, "strerror", None) or error), "warn")
        return {}
    choices = data.get("no_pack", {}) if isinstance(data, dict) else {}
    return choices if isinstance(choices, dict) else {}


def _save_server_choice(key, choice):
    path = os.path.join(engine.data_dir(), PREFERENCES_FILE)
    choices = _server_preferences()
    choices[key] = choice
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        engine.write_json(path, {"no_pack": choices})
    except OSError as error:
        _log("Modpacks: couldn't save the choice for %s (%s); you'll be asked again"
             % (key, getattr(error, "strerror", None) or error), "warn")


def _open_modpacks(game_dir):
    root = _app()
    if root is not None:
        window.open_window(root, game_dir)


def _server_pack_choice(game_dir, target):
    key = _server_key(target)
    choice = _server_preferences().get(key)
    if choice == "default":
        return True
    if choice == "setup":
        _open_modpacks(game_dir)
        raise engine.ModpacksError("Launch cancelled. Set up a modpack for %s, then join again." % key)
    if threading.current_thread() is not threading.main_thread():
        _log("Modpacks: no pack is set up for %s; continuing with the default Mods folder" % key, "warn")
        return True
    dialog = window.NoPackDialog(_app(), key)
    dialog.wait_window()
    if dialog.dont_ask:
        _save_server_choice(key, dialog.choice)
    if dialog.choice == "setup":
        _open_modpacks(game_dir)
        raise engine.ModpacksError("Launch cancelled. Set up a modpack for %s, then join again." % key)
    return True


def _has_active_pack(game_dir, target):
    return any(pack.links(target, engine.resolve) for pack in engine.list_packs(game_dir))


def _update_prompt(blocks, has_unlisted, context, has_deletions=False,
                   has_protected=False, has_dropped=False):
    title = "Modpack changes" if has_unlisted else "Modpack updates"
    message = "Changes are ready:\n\n" + "\n\n".join(blocks)
    if has_unlisted:
        message += ("\n\nThe update link no longer lists the mods shown under "
                    "\"Removed from link\".")
        if has_deletions:
            message += " Accepting removes unchanged copies from the pack."
        if has_protected:
            message += " Launcher-managed files are reported but never installed or removed by Modpacks."
    if has_dropped:
        message += ("\n\nMods marked \"Back on link, still skipped because you removed it\" "
                    "remain excluded; add them back to the pack if you want to use them.")
    message += "\n\nApply these changes%s?" % context
    return title, message


def _check_join_updates(game_dir, target):
    if threading.current_thread() is not threading.main_thread():
        return
    report = source.check_target(game_dir, target)
    waiting, blocks = [], []
    for name, manifest, found, problem in report:
        if problem:
            _log("Modpacks: couldn't quickly check %s before joining (%s); using installed files"
                 % (name, problem), "warn")
        elif source.count(found):
            waiting.append((name, manifest, found, None))
            pack = engine.find_pack(game_dir, name)
            lines = source.preview(pack, found) if pack is not None else [source.describe(found)]
            blocks.append(name + "\n" + "\n".join("    " + line for line in lines))
    if not waiting:
        return
    server = _server_key(target)
    has_unlisted = any(found.get("unlisted", found["removed"]) for _, _, found, _ in waiting)
    has_deletions = any(found["removed"] for _, _, found, _ in waiting)
    has_protected = any(found.get("protected") or found.get("managed_added")
                        for _, _, found, _ in waiting)
    has_dropped = any(found.get("dropped") for _, _, found, _ in waiting)
    title, prompt = _update_prompt(blocks, has_unlisted, " before joining %s" % server,
                                  has_deletions, has_protected, has_dropped)
    if not messagebox.askyesno(title, prompt, parent=_app()):
        _log("Modpacks: updates skipped before joining %s; using installed files" % server, "warn")
        return
    for name, found, problem in source.update_all(game_dir, waiting):
        if problem:
            _log("Modpacks: %s wasn't updated before joining (%s); using installed files"
                 % (name, problem), "err")
        else:
            _log("Modpacks: %s: %s" % (name, source.summary(found)), "ok")


def _apply(game_dir, target):
    leave_out = None
    removed = False
    while True:
        try:
            return engine.apply(game_dir, target, leave_out=leave_out)
        except engine.ConflictError as error:
            loose = [] if removed else engine.loose_in(error.conflicts)
            choice = None
            if leave_out is None and threading.current_thread() is threading.main_thread():
                try:
                    choice = ask_conflict(error.conflicts, loose)
                except Exception:
                    choice = None
            if choice is None:
                _alert("Modpacks", "The game can't start with these mods together:\n\n"
                       + "\n".join("• " + c for c in error.conflicts)
                       + "\n\nOpen Modpacks to fix it, then join again.")
            if choice == "remove" and loose:
                for name in engine.remove_loose(game_dir, loose):
                    _log("Modpacks: %s went to the Recycle Bin" % name, "warn")
                removed = True
            elif choice == "run":
                leave_out = engine.left_out(error.conflicts)
            else:
                raise


def before_launch(args):
    if not _is_game(args):
        return
    game_dir = os.path.dirname(os.path.abspath(os.fspath(args[0])))
    target = engine.target_from_args(list(args))
    try:
        if target is not None and not _has_active_pack(game_dir, target):
            _server_pack_choice(game_dir, target)
        if target is not None:
            _check_join_updates(game_dir, target)
        result = _apply(game_dir, target)
    except engine.ConflictError as error:
        raise engine.ModpacksError("Modpacks found a conflict, so the game didn't start. "
                                   + "; ".join(error.conflicts)) from None
    except engine.ModpacksError as error:
        _alert("Modpacks", str(error))
        raise engine.ModpacksError("Modpacks: " + str(error)) from None
    except Exception as error:
        message = "Modpacks hit an error and stopped the launch: %r. Untick Modpacks in Addons to launch without it." % (error,)
        _alert("Modpacks", message)
        raise engine.ModpacksError(message) from None
    if result.skipped == "running":
        _log("Modpacks: the game is already running, so modpacks are unchanged", "warn")
        return
    if result.skipped == "no-mods":
        _log("Modpacks: there's no Mods folder yet, so modpacks are unchanged", "warn")
        return
    for note in result.plan.notes:
        _log("Modpacks: " + note, "warn")
    for override in result.plan.overrides:
        _log("Modpacks: using %s's %s instead of the shared copy from %s for this server"
             % (engine.listed(override["server_packs"]), override["file"],
                engine.listed(override["shared_packs"])), "info")
    for path in result.kept_aside:
        _log("Modpacks: no pack holds %s anymore, so it was kept in %s"
             % (os.path.basename(path), os.path.join(engine.PACKS_DIR, engine.REMOVED_DIR)), "warn")
    for name in result.shadowed:
        _log("Modpacks: using the linked pack's %s for this server; your loose copy is saved in %s"
             % (name, os.path.join(engine.PACKS_DIR, engine.SHADOWED_DIR)), "warn")
    for warning in result.warnings:
        _log("Modpacks: " + warning, "warn")
    if result.plan.left_out:
        _log("Modpacks: left out for this launch: " + engine.describe_left_out(result.plan.left_out), "warn")
    _log(result.plan.summary(), "info")
    window_open = getattr(_app(), "_modpacks_window", None)
    try:
        if window_open is not None and window_open.winfo_exists():
            window_open.after_idle(window_open.refresh)
    except Exception:
        pass


class _Popen(subprocess.Popen):
    _modpacks = True

    def __init__(self, args, *rest, **kwargs):
        before_launch(args)
        super().__init__(args, *rest, **kwargs)


if not getattr(subprocess.Popen, "_modpacks", False):
    subprocess.Popen = _Popen


def _game_dir(root):
    try:
        exe = root.v_exe.get().strip()
    except Exception:
        exe = ""
    if not exe or not os.path.isfile(exe):
        messagebox.showerror("Game not found",
                             "Please set the path to 'A Township Tale.exe' above first.", parent=root)
        return None
    game_dir = os.path.dirname(exe)
    if not os.path.isdir(os.path.join(game_dir, "Mods")):
        messagebox.showinfo("No Mods folder",
                            "There is no Mods folder next to the game yet. Install MelonLoader with the "
                            "Mods button, then start the game once.", parent=root)
        return None
    return game_dir


def _open(root):
    game_dir = _game_dir(root)
    if game_dir:
        window.open_window(root, game_dir)


def _add_button(anchor):
    try:
        root = anchor.winfo_toplevel()
        if not hasattr(root, "v_exe"):
            return
        after = anchor
        for child in anchor.master.pack_slaves():
            try:
                text = child.cget("text")
            except Exception:
                continue
            if text == BUTTON_TEXT:
                return
            if text in AFTER_TEXTS:
                after = child
        for child in anchor.master.winfo_children():
            try:
                if child.cget("text") == BUTTON_TEXT:
                    return
            except Exception:
                continue
        button = _theme._btn(anchor.master, BUTTON_TEXT, lambda: _open(root),
                             font=("Segoe UI", 9), pady=5, padx=10)
        button.pack(side="left", padx=(6, 0), after=after)
        if not getattr(root, "_modpacks_checked", False):
            root._modpacks_checked = True
            root.after(CHECK_DELAY_MS, lambda: _startup_check(root))
    except Exception:
        pass


def _startup_check(root):
    try:
        exe = root.v_exe.get().strip()
    except Exception:
        return
    game_dir = os.path.dirname(exe)
    if not exe or not os.path.isdir(os.path.join(game_dir, "Mods")) or not source.linked_packs(game_dir):
        return
    window.background(root, lambda: source.check_all(game_dir), lambda report, error: _checked(root, game_dir, report, error))


def _checked(root, game_dir, report, error):
    if error or not report:
        return
    waiting, blocks = [], []
    for name, manifest, found, problem in report:
        if problem:
            _log("Modpacks: couldn't check %s for updates (%s)" % (name, problem), "warn")
        elif source.count(found):
            waiting.append("%s: %s" % (name, source.describe(found)))
            pack = engine.find_pack(game_dir, name)
            lines = source.preview(pack, found) if pack is not None else [source.describe(found)]
            blocks.append(name + "\n" + "\n".join("    " + line for line in lines))
        else:
            pack = engine.find_pack(game_dir, name)
            if pack is not None:
                try:
                    source.record_manifest(pack, manifest)
                except OSError as save_error:
                    _log("Modpacks: couldn't save the checked file list for %s (%s)"
                         % (name, engine._why(save_error)), "warn")
    if not waiting:
        if not any(r[3] for r in report):
            _log("Modpacks: linked packs are up to date", "dim")
        return
    valid = [(found, problem) for _, _, found, problem in report if not problem and found is not None]
    has_unlisted = any(found.get("unlisted", found["removed"]) for found, _ in valid)
    has_deletions = any(found["removed"] for found, _ in valid)
    has_protected = any(found.get("protected") or found.get("managed_added") for found, _ in valid)
    has_dropped = any(found.get("dropped") for found, _ in valid)
    title, prompt = _update_prompt(blocks, has_unlisted, " now", has_deletions,
                                  has_protected, has_dropped)
    if not messagebox.askyesno(title, prompt, parent=root):
        _log("Modpacks: updates skipped for now (" + "; ".join(waiting) + ")", "warn")
        return
    window.background(root, lambda: source.update_all(game_dir, report), lambda done, error: _updated(root, done, error))


def _updated(root, done, error):
    if error:
        _log("Modpacks: updates failed (%s)" % error, "err")
        return
    for name, found, problem in done or []:
        if problem:
            _log("Modpacks: %s wasn't updated (%s)" % (name, problem), "err")
        else:
            _log("Modpacks: %s: %s" % (name, source.summary(found)), "ok")
    window_open = getattr(root, "_modpacks_window", None)
    try:
        if window_open is not None and window_open.winfo_exists():
            window_open.refresh()
    except Exception:
        pass


def _wrap(orig):
    def _btn(parent, text, cmd, style="normal", **kw):
        button = orig(parent, text, cmd, style, **kw)
        if text == MODS_BUTTON:
            button.after_idle(lambda: button.after_idle(lambda: _add_button(button)))
        return button
    return _btn


_theme._btn = _wrap(_theme._btn)

for _name in ("client.core.launcher_window",):
    _module = sys.modules.get(_name)
    if _module is not None and callable(getattr(_module, "_btn", None)):
        _module._btn = _wrap(_module._btn)
