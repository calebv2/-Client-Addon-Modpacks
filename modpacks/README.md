# Modpacks

A TavernLauncher addon for keeping your mods in packs and turning each pack on where you want it.
Handy when you play on servers that need different mods.

Click **📦 Modpacks** next to 🧪 Mods on the main window.

## How it works

- A pack is a folder of mods in `Modpacks` next to the game, like `Modpacks\QoL\`.
- Each pack is **Off**, **On everywhere**, or **Only on these servers**. Pick the servers from your
  saved and recent servers, or add the one in the IP box.
- For mods needed on most or all servers, make a shared pack and set it to **On everywhere**.
  It loads alongside the server's own pack.
- If a server-specific pack and an **On everywhere** pack contain different versions with the same filename,
  the server-specific copy wins for that server. The shared copy remains in its pack and is used on other
  servers. Different versions in two packs both scoped to the same server still need to be resolved.
- Several packs can be on at once. Untick a mod to leave it out of its pack without removing it.
- When the game starts, Modpacks copies the mods of every pack that's on for that server into `Mods`,
  and takes out the ones it put there before that aren't wanted now.
- Loose mods in `Mods` are yours and otherwise always load. Modpacks leaves them alone except when an active
  linked pack has the same filename, as described below.
- While a pack is on, its mods sit in `Mods` next to your loose mods. The window's loose list shows only yours.
- When a linked pack is on for a server, its copy wins over a different loose DLL with the same filename.
  Modpacks saves the loose copy in `Modpacks\_shadowed` and restores it on a later launch when no active pack
  uses that filename.
- A Main Menu launch loads only the packs that are On everywhere.
- The first time you join a server with no active pack, Modpacks asks whether to use the default `Mods` folder.
  You can remember either choice for that server. Choosing to set up a pack opens Modpacks and cancels that launch.
- The launcher log says which packs loaded, like `Modpacks: QoL, Velaris (12 mods)`.

## The window

- **Packs:** New, Rename, Delete (to the Recycle Bin), Import zip.
- **The selected pack:** its mode, its servers, its mods with their real names, Open folder,
  Add files..., Export zip.
- **Next launch:** exactly what will load for the server in the IP box. Problems show in red. If a loose
  mod is in the way, **Remove loose copy** sends it to the Recycle Bin.
- **Each mod in a pack:** **Move to loose** takes it out of the pack and makes it a loose mod. **Delete** sends
  it to the Recycle Bin. If the same mod is in other packs, you can delete it there too in the same step.
- **Loose mods:** what's loose in `Mods`. Each one has **Add to pack...** and **Delete**.
  **Add loose mods...** copies the DLLs you pick into `Mods`. **Make a pack from these...** moves the mods
  you pick into a pack. New packs start On everywhere, so nothing changes until you change the mode.

## Packs from a link

Some servers publish their client mods as a list, like Sheikah Sanctuary's
`https://att.aservertown.fans/manifest.json`. Click **From a link...**, paste the link, and Modpacks makes a pack
that downloads those mods and keeps them up to date.

- If the link's site is one of your saved or recent servers, the pack loads only on that server.
- When the launcher opens, Modpacks checks linked packs concurrently and asks before applying changes. It also
  checks the relevant linked pack again just before joining, so changes made while the launcher was open can be
  installed before you connect. Manifest requests bypass HTTP and URL-keyed caches so checks see the latest list.
  If that quick check can't reach the update site, the current pack is used.
- The update preview includes mods added to or removed from the link, including files managed by the launcher.
  Accepting removes unchanged linked files that are no longer listed; files you changed yourself are kept.
  `TavernEssentials.dll` in a pack or linked manifest is handled like any other pack mod and is installed,
  updated, and removed with that pack. A loose copy is also handled like any other loose mod.
- Every file must come over https from the same site as the list and match its SHA-256, or nothing changes.
- Mods you add to a linked pack yourself, or files you change, are never overwritten.
- A mod you delete or move out of a linked pack isn't downloaded again. Add it back with **Add files...**.
- Updates name every mod, like `updated DyeingAPI 1.0.0 to 1.1.0, new NightSkeletons 1.0.0`.
- **Check now** and **Remove link** are on the pack. If you used a separate updater addon for the same mods,
  turn it off so the mods aren't installed twice.

A list is JSON in this shape. `name` and `servers` are optional:

```json
{
  "name": "My Server",
  "servers": ["myserver.com:1757"],
  "files": [
    {"filename": "MyMod.dll", "sha256": "<64 hex characters>", "url": "https://myserver.com/MyMod.dll"}
  ]
}
```

## Good to know

- If two packs have different files with the same name, a pack mod is also loose in `Mods` as a
  different file (unless an active linked pack provides it), or the same mod would load twice, Modpacks
  asks what to do before the game starts:
  - **Remove and play** sends the loose files in the way to the Recycle Bin, then starts the game.
    It only shows when a loose file is part of the problem.
  - **Run anyway** starts the game without the clashing pack mods, this time only. Your loose mods stay.
  - **Close** stops the launch and changes nothing.
- If the game is already running, nothing changes and the launch goes ahead as usual.
- If a file can't be changed, everything is put back the way it was and the launch stops.
- A mod that no pack holds anymore is never deleted. It's kept in `Modpacks\_removed`. Mods you delete
  yourself go to the Recycle Bin, along with their copy in `Mods`.
- Moving mods and adding or deleting loose mods need the game closed, because it holds the mods open.
  Deleting a mod from a pack works anytime. Its copy leaves `Mods` the next time the game starts.
- Config files and `UserData` are shared by all packs.
- Circuit's Voice Chat belongs to the launcher and is never managed by Modpacks. `TavernEssentials.dll` can
  be included in a pack or managed as a loose mod like any other mod.
- Export leaves out your mode and servers. An imported pack starts Off, and mods named
  `*.Server.dll` come in unticked because they look like server mods.

## Install

1. Copy the `modpacks` folder into the `addons` folder next to `TavernLauncher - Client.exe`.
2. Open TavernLauncher, click **🧩 Addons**, tick **Modpacks**, click **Save**, then restart the launcher.
