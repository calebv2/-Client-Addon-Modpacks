import json
import os
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

from . import engine

USER_AGENT = "TavernLauncher-Modpacks/1.0"
TIMEOUT = 20
STARTUP_CHECK_TIMEOUT = 8
JOIN_CHECK_TIMEOUT = 4
MAX_MANIFEST = 2 * 1024 * 1024
MAX_FILES = 300
HEX = re.compile(r"^[0-9A-Fa-f]{64}$")
DOWNLOADS_DIR = "_downloads"
SEEN_LIMIT = 400


class SourceError(engine.ModpacksError):
    pass


def check_url(url):
    parsed = urlparse(str(url).strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise SourceError("Update links must start with https://")
    return parsed


def fetch(url, limit, timeout=TIMEOUT, cache_bust=False):
    check_url(url)
    request_url = url
    if cache_bust:
        parsed = urlparse(url)
        query = parse_qsl(parsed.query, keep_blank_values=True)
        query.append(("_modpacks_check", str(time.time_ns())))
        request_url = urlunparse(parsed._replace(query=urlencode(query)))
    request = Request(request_url, headers={
        "User-Agent": USER_AGENT,
        "Cache-Control": "no-cache, no-store, max-age=0",
        "Pragma": "no-cache",
    })
    with urlopen(request, timeout=timeout) as response:
        final = response.geturl()
        data = response.read(limit + 1)
    if len(data) > limit:
        raise SourceError("%s is too big." % os.path.basename(urlparse(url).path) or url)
    return final, data


def host_of(url):
    return (urlparse(url).hostname or "").lower()


class Manifest:
    def __init__(self, url, files, name, servers, skipped, listed=None):
        self.url = url
        self.host = host_of(url)
        self.files = files
        self.name = name
        self.servers = servers
        self.skipped = skipped
        self.listed = listed if listed is not None else {
            item["filename"].lower(): item["filename"] for item in files}


def read_manifest(url, fetcher=None, timeout=TIMEOUT):
    check_url(url)
    try:
        final, data = (fetch(url, MAX_MANIFEST, timeout, cache_bust=True) if fetcher is None
                       else fetcher(url, MAX_MANIFEST))
    except SourceError:
        raise
    except Exception as error:
        raise SourceError("Couldn't read the update link (%s)." % (getattr(error, "reason", None) or error))
    final_parsed = urlparse(final)
    if final_parsed.scheme != "https" or (final_parsed.hostname or "").lower() != host_of(url):
        raise SourceError("The update link moved to another site, so it was refused.")
    try:
        manifest = json.loads(data.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        raise SourceError("The update link didn't return a mod list.")
    entries = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(entries, list):
        raise SourceError("The update link didn't return a mod list.")
    if len(entries) > MAX_FILES:
        raise SourceError("The mod list is too long.")
    files, skipped, seen, listed = [], [], set(), {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise SourceError("The mod list has an entry Modpacks can't read.")
        name = entry.get("filename")
        digest = entry.get("sha256")
        link = entry.get("url")
        if (not isinstance(name, str) or not engine.is_dll(name) or "/" in name or "\\" in name
                or ":" in name or name in (".", "..") or engine.check_name(name.rsplit(".", 1)[0] or "x")):
            raise SourceError("The mod list has an unsafe file name: %r" % (name,))
        if not isinstance(digest, str) or not HEX.match(digest):
            raise SourceError("The mod list has no valid SHA-256 for %s." % name)
        if not isinstance(link, str):
            raise SourceError("The mod list has no download link for %s." % name)
        resolved = urljoin(final, link)
        parsed = urlparse(resolved)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() != host_of(url):
            raise SourceError("%s downloads from another site, so the list was refused." % name)
        if name.lower() in seen:
            raise SourceError("The mod list has %s twice." % name)
        seen.add(name.lower())
        listed[name.lower()] = name
        if engine.is_pack_managed_mod(name):
            skipped.append("%s (the launcher manages it)" % name)
            continue
        files.append({"filename": name, "sha256": digest.upper(), "url": resolved})
    title = manifest.get("name") if isinstance(manifest.get("name"), str) else ""
    servers = [s for s in engine._list(manifest.get("servers")) if isinstance(s, str) and s.strip()]
    return Manifest(url, files, title.strip()[:60], servers, skipped, listed)


def source_state(pack):
    data = pack.source if isinstance(pack.source, dict) else {}
    files = data.get("files") if isinstance(data.get("files"), dict) else {}
    listed = data.get("listed")
    if isinstance(listed, dict):
        listed = {k.lower(): v for k, v in listed.items() if isinstance(k, str) and isinstance(v, str)}
    else:
        names = listed if isinstance(listed, list) else list(files)
        listed = {name.lower(): name for name in names if isinstance(name, str)}
    return {"url": data.get("url") if isinstance(data.get("url"), str) else "",
            "files": {k.lower(): str(v).upper() for k, v in files.items() if isinstance(k, str)},
            "listed": listed,
            "seen": [str(s).upper() for s in engine._list(data.get("seen"))],
            "checked": data.get("checked") if isinstance(data.get("checked"), str) else ""}


def changes(pack, manifest):
    state = source_state(pack)
    present = {n.lower(): n for n in pack.files()}
    new, updated, removed, unlisted, managed_added, kept, dropped = [], [], [], [], [], [], []
    manifest_listed = getattr(manifest, "listed", None)
    if not isinstance(manifest_listed, dict):
        manifest_listed = {item["filename"].lower(): item["filename"] for item in manifest.files}
    downloadable = {item["filename"].lower() for item in manifest.files}
    managed_added = [name for low, name in manifest_listed.items()
                     if low not in state["listed"] and low not in downloadable
                     and engine.is_pack_managed_mod(name)]
    listed = set()
    for item in manifest.files:
        low = item["filename"].lower()
        listed.add(low)
        if low not in present:
            (dropped if low in pack.dropped else new).append(item)
            continue
        current = engine.sha256(os.path.join(pack.path, present[low]))
        if current == item["sha256"]:
            if state["files"].get(low) != current:
                new.append(item)
            continue
        recorded = state["files"].get(low)
        if recorded is not None and recorded != current:
            kept.append("%s (you changed it, so it wasn't updated)" % present[low])
            continue
        if recorded is None and state["url"]:
            kept.append("%s (it's yours, not from the update link)" % present[low])
            continue
        updated.append(item)
    protected = []
    for low, old_name in state["listed"].items():
        if low in listed or low in manifest_listed:
            continue
        name = present.get(low)
        unlisted.append(name or old_name)
        if engine.is_pack_managed_mod(old_name):
            protected.append(name or old_name)
            kept.append("%s is managed by the launcher and was left alone" % (name or old_name))
            continue
        if name is None:
            continue
        recorded = state["files"].get(low)
        if recorded is None:
            kept.append("%s is no longer listed, but its ownership can't be verified; it was left in the pack"
                        % name)
            continue
        current = engine.sha256(os.path.join(pack.path, name))
        if current == recorded:
            removed.append(name)
        else:
            kept.append("%s is no longer listed, but you changed it, so it was left in the pack" % name)
    return {"new": new, "updated": updated, "removed": removed, "unlisted": unlisted,
            "protected": protected, "managed_added": managed_added, "kept": kept,
            "dropped": [item["filename"] for item in dropped]}


def record_manifest(pack, manifest):
    listed = getattr(manifest, "listed", None)
    if not isinstance(listed, dict):
        listed = {item["filename"].lower(): item["filename"] for item in manifest.files}
    listed = dict(listed)
    state = source_state(pack)
    if state["listed"] == listed:
        return False
    pack.source = dict(pack.source) if isinstance(pack.source, dict) else {}
    pack.source["listed"] = listed
    pack.save()
    return True


def count(found):
    return (len(found["new"]) + len(found["updated"])
            + len(found.get("unlisted", found["removed"])) + len(found.get("dropped", []))
            + len(found.get("managed_added", [])))


def _title(info, file_name):
    if info and info["name"]:
        return info["name"]
    return file_name[:-4] if engine.is_dll(file_name) else file_name


def preview(pack, found):
    present = {n.lower(): n for n in pack.files()}

    def held(name):
        file_name = present.get(name.lower(), name)
        info = engine.mod_info(os.path.join(pack.path, file_name))
        if not info or not info["name"]:
            return file_name
        text = info["name"] + (" " + info["version"] if info["version"] else "")
        if _title(None, file_name).lower() != info["name"].lower():
            text += " (%s)" % file_name
        return text

    lines = []
    for label, names in (("New", [item["filename"] for item in found["new"]]),
                         ("Updated", [held(item["filename"]) for item in found["updated"]]),
                         ("Listed by link, managed by launcher", found.get("managed_added") or []),
                         ("Removed from link", [held(name) for name in found.get("unlisted", found["removed"])]),
                         ("Back on link, still skipped because you removed it",
                          found.get("dropped") or [])):
        if names:
            lines.append("%s: %s" % (label, ", ".join(names)))
    return lines


def summary(found):
    info = found.get("info") or {}
    parts = []
    for item in found["updated"]:
        old, new = info.get(item["filename"].lower(), (None, None))
        before = old["version"] if old else ""
        after = new["version"] if new else ""
        text = "updated " + _title(new or old, item["filename"])
        if before and after and before != after:
            text += " %s to %s" % (before, after)
        elif after:
            text += " " + after
        parts.append(text)
    for item in found["new"]:
        new = info.get(item["filename"].lower(), (None, None))[1]
        parts.append("new " + _title(new, item["filename"]) + (" " + new["version"] if new and new["version"] else ""))
    for name in found["removed"]:
        parts.append("removed " + _title(info.get(name.lower(), (None, None))[0], name))
    actual_removed = {name.lower() for name in found["removed"]}
    for name in found.get("unlisted", []):
        if name.lower() not in actual_removed:
            text = "no longer listed " + _title(None, name)
            if name.lower() in {n.lower() for n in found.get("protected", [])}:
                text += " (launcher-managed; left alone)"
            parts.append(text)
    if found.get("dropped"):
        parts.append("back on link, still skipped: " + ", ".join(found["dropped"]))
    if found.get("managed_added"):
        parts.append("listed by link, managed by launcher: " + ", ".join(found["managed_added"]))
    return ", ".join(parts) or "up to date"


def describe(found):
    parts = []
    for key, word in (("new", "new"), ("updated", "updated")):
        if found[key]:
            parts.append("%d %s" % (len(found[key]), word))
    removals = found.get("unlisted", found["removed"])
    if removals:
        parts.append("%d removed from link" % len(removals))
    if found.get("dropped"):
        parts.append("%d back on link but still skipped" % len(found["dropped"]))
    if found.get("managed_added"):
        parts.append("%d listed, managed by launcher" % len(found["managed_added"]))
    return ", ".join(parts) or "up to date"


def _local_copy(game_dir, name, digest):
    candidates = [os.path.join(game_dir, "Mods", name)]
    for pack in engine.list_packs(game_dir):
        candidates.append(os.path.join(pack.path, name))
    for path in candidates:
        try:
            if os.path.isfile(path) and engine.sha256(path) == digest:
                return path
        except OSError:
            continue
    return None


def _download(item, target, game_dir, fetcher):
    local = _local_copy(game_dir, item["filename"], item["sha256"])
    if local:
        shutil.copyfile(local, target)
    else:
        try:
            final, data = (fetcher or fetch)(item["url"], engine.MAX_ENTRY)
        except SourceError:
            raise
        except Exception as error:
            raise SourceError("%s couldn't be downloaded (%s)." % (
                item["filename"], getattr(error, "reason", None) or error))
        parsed = urlparse(final)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() != host_of(item["url"]):
            raise SourceError("%s moved to another site, so it was refused." % item["filename"])
        with open(target, "wb") as f:
            f.write(data)
    if engine.sha256(target) != item["sha256"]:
        raise SourceError("%s didn't match its SHA-256, so nothing was changed." % item["filename"])


def sync(game_dir, pack, manifest, fetcher=None):
    found = changes(pack, manifest)
    work = engine.unique_path(os.path.join(engine.packs_dir(game_dir), DOWNLOADS_DIR), pack.name)
    os.makedirs(work)
    try:
        for item in found["new"] + found["updated"]:
            _download(item, os.path.join(work, item["filename"]), game_dir, fetcher)
        state = source_state(pack)
        present = {n.lower(): n for n in pack.files()}
        info = {}
        for item in found["new"] + found["updated"]:
            low = item["filename"].lower()
            old = engine.mod_info(os.path.join(pack.path, present[low])) if low in present else None
            info[low] = (old, engine.mod_info(os.path.join(work, item["filename"])))
        for name in found["removed"]:
            info[name.lower()] = (engine.mod_info(os.path.join(pack.path, name)), None)
        found["info"] = info
        seen = list(state["seen"])
        for item in found["updated"]:
            old = os.path.join(pack.path, present[item["filename"].lower()])
            seen.append(engine.sha256(old))
        for name in found["removed"]:
            seen.append(engine.sha256(os.path.join(pack.path, name)))
        for item in found["new"] + found["updated"]:
            low = item["filename"].lower()
            target = os.path.join(pack.path, present.get(low, item["filename"]))
            os.replace(os.path.join(work, item["filename"]), target)
            if engine.looks_server_only(item["filename"]) and low not in present:
                pack.on.discard(low)
        for name in found["removed"]:
            os.remove(os.path.join(pack.path, name))
        files = {}
        for item in manifest.files:
            path = os.path.join(pack.path, item["filename"])
            if os.path.isfile(path) and engine.sha256(path) == item["sha256"]:
                files[item["filename"].lower()] = item["sha256"]
        for item in manifest.files:
            low = item["filename"].lower()
            if low not in files and low in state["files"]:
                files[low] = state["files"][low]
        seen.extend(item["sha256"] for item in manifest.files)
        listed = getattr(manifest, "listed", None)
        if not isinstance(listed, dict):
            listed = {item["filename"].lower(): item["filename"] for item in manifest.files}
        pack.source = {"url": manifest.url, "files": files,
                       "listed": dict(listed),
                       "seen": _trim(seen),
                       "checked": time.strftime("%Y-%m-%d %H:%M")}
        pack.save()
    except OSError as error:
        raise SourceError("The update couldn't be saved (%s)." % engine._why(error))
    finally:
        shutil.rmtree(work, ignore_errors=True)
        try:
            os.rmdir(os.path.dirname(work))
        except OSError:
            pass
    return found


def _trim(seen):
    unique = list(dict.fromkeys(seen))
    return unique[-SEEN_LIMIT:]


def suggest_link(manifest, known_servers):
    wanted = []
    for entry in manifest.servers:
        host, _, port = entry.strip().rpartition(":")
        if not host or not port.isdigit():
            host, port = entry.strip(), str(engine.DEFAULT_PORT)
        wanted.append(engine.server_key(host, port))
    if not wanted:
        wanted = [engine.server_key(manifest.host, engine.DEFAULT_PORT)]
    links = []
    for server in known_servers:
        key = engine.server_key(server["host"], server["port"])
        if key in wanted and all(engine.server_key(s["host"], s["port"]) != key for s in links):
            links.append(server)
    return links


def subscribe(game_dir, url, name, known_servers, fetcher=None, manifest=None):
    manifest = manifest or read_manifest(url, fetcher)
    pack = engine.create_pack(game_dir, name, "off")
    try:
        links = suggest_link(manifest, known_servers)
        if links:
            pack.mode = "servers"
            pack.servers = [{"name": s["name"], "host": s["host"], "port": str(engine.parse_port(s["port"]))}
                            for s in links]
        pack.source = {"url": manifest.url, "files": {}, "seen": [], "checked": ""}
        pack.save()
        found = sync(game_dir, pack, manifest, fetcher)
    except Exception:
        shutil.rmtree(pack.path, ignore_errors=True)
        raise
    return engine.find_pack(game_dir, pack.name), found, manifest


def linked_packs(game_dir):
    return [p for p in engine.list_packs(game_dir) if source_state(p)["url"]]


def check_all(game_dir, fetcher=None):
    packs = linked_packs(game_dir)
    if not packs:
        return []
    with ThreadPoolExecutor(max_workers=min(4, len(packs))) as executor:
        return list(executor.map(lambda pack: _check_pack(pack, fetcher), packs))


def _check_pack(pack, fetcher=None, timeout=STARTUP_CHECK_TIMEOUT):
    try:
        manifest = read_manifest(source_state(pack)["url"], fetcher, timeout)
        return pack.name, manifest, changes(pack, manifest), None
    except Exception as error:
        return pack.name, None, None, str(error)


def check_target(game_dir, target, fetcher=None, timeout=JOIN_CHECK_TIMEOUT):
    if target is None:
        return []
    packs = [pack for pack in linked_packs(game_dir) if pack.links(target, engine.resolve)]
    if not packs:
        return []
    with ThreadPoolExecutor(max_workers=min(4, len(packs))) as executor:
        return list(executor.map(lambda pack: _check_pack(pack, fetcher, timeout), packs))


def update_all(game_dir, report, fetcher=None):
    done = []
    for name, manifest, found, error in report:
        if error or manifest is None or not count(found):
            continue
        pack = engine.find_pack(game_dir, name)
        if pack is None:
            continue
        try:
            done.append((name, sync(game_dir, pack, manifest, fetcher), None))
        except Exception as error:
            done.append((name, None, str(error)))
    return done


def unlink(pack):
    pack.source = {}
    pack.save()
