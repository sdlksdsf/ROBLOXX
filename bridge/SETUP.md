# Setup guide — Arena agent ⇄ your Roblox Studio (current Studio builds)

There is **no “Security” tab** in modern Studio settings — that is old advice you'll see all
over the internet. The switch you need now lives in the **Assistant panel**. This guide follows
Roblox's own docs (checked 2026‑11‑06).

Time: ~5 minutes.

---

## Step 1 — Update Studio and open a place

1. Open Roblox Studio. If it offers an update, **let it update and restart** — the MCP server
   ships inside Studio and older builds don't have it in the right place.
2. Open a saved place (or **New ▸ Baseplate**, then save it).
   A place must be **open**; the “Start Page” is not enough.

---

## Step 2 — Turn the MCP server on (this replaces the old “Security” step)

1. In Studio, look at the **top bar**: there is an **Assistant** button on the right side.
   If you don't see it, open it from **View ▸ Assistant** (or the Assistant icon in the ribbon).
2. In the Assistant window, click the **⋯** (three dots) menu.
3. Choose **Manage MCP Servers** (newer builds label it **Settings ▸ MCP Servers**).
4. Turn on **Enable Studio as MCP server**.
5. Leave that panel open for a second — it shows a **green indicator with a client count** once
   something connects. That is your proof of life.

> In your screenshot the settings dialog only had Studio / Selection / Script Editor /
> Diagnostics / Network / Physics / Rendering. That is the normal newer layout: port permission
> and MCP are handled by the Assistant panel now, not by that list.

---

## Step 3 — *(only if you want uploads/publishing)* allow Cloud actions

Roblox added `open_cloud_invoke`, which lets an MCP client call Open Cloud **without an API key**.
It is gated by per‑game permissions:

1. Click **MCP** in the **bottom‑right corner** of Studio → tab **MCP Permissions**
   (or Assistant ▸ ⋯ ▸ Settings ▸ **MCP Permissions**).
2. Pick an access level — **Balanced** is a good start; **Full access** only if you want me to
   publish to a live game.
3. If you plan to upload images/models, open **Upload folders** and approve the folder your
   files live in, e.g. `C:\Users\you\Pictures\Roblox`.
4. The place must belong to a **published game** (unpublished local places can't use these).

Your Roblox API key is already installed as a fallback, so if you skip this step uploads and
publishing still work through the relay — they just go over the API key instead of your Studio
session.

---

## Step 4 — Run the installer (PowerShell)

`Win` → type `powershell` → Enter → paste:

```powershell
iwr -UseBasicParsing "https://8788-iy8w5dbt2nw4x9qqg772j.e2b.app/relay/file/setup.ps1" -OutFile "$env:TEMP\arena.ps1"; & "$env:TEMP\arena.ps1"
```

It prints a **preflight table** like this:

```
  PREFLIGHT REPORT
  ----------------
  [PASS] PowerShell               v5.1.x
  [PASS] Roblox Studio running    pid 12345
  [PASS] mcp.bat present          C:\Users\you\AppData\Local\Roblox\mcp.bat
  [PASS] StudioMCP.exe present    ...\Versions\version-xxxx\StudioMCP.exe
  [PASS] bridge reachable         HTTP 200 -> https://8788-....e2b.app
  [PASS] Roblox API key file      ...\ArenaRoblox\roblox_api_key.txt
  [PASS] Cloud API auth           HTTP 404 (401/403 means the key is invalid)
```

`[FAIL] port 13469 listening` is **normal** before the first call in modern Studio builds —
Studio only opens that socket when a client actually connects, and the relay connects by itself.
Everything else should be PASS.

Then the relay window opens and prints:

```
[OK] studio transport: stdio ...\mcp.bat (pid 4242)
[OK] joined bridge as relay-1794123456-ab12cd
[OK] READY - waiting for orders from the Arena agent (Ctrl+C to stop)
```

**Leave that window open.** In Roblox Studio, the **Manage MCP Servers** panel should now show a
green indicator with **1** client. That is the real confirmation.

---

## Step 5 — Say `connected` in chat

I will then run, in order:

```
python3 roblox.py doctor      # every hop, plus the tool list Studio advertises
python3 roblox.py state       # play state / datamodel types
python3 roblox.py code "print('connected')"
```

You will see `connected` in Studio's **View ▸ Output** window. From then on, talk normally.

---

## Step 6 — What you can ask for

| You say | What I call |
|---|---|
| "build a 20×20 spawn platform at 0,0,0 with a red neon ring" | `execute_luau` |
| "read ServerScriptService.Main and fix the bug" | `script_read`, `multi_edit` |
| "find every script that mentions DataStore" | `script_grep` |
| "what's inside workspace.SpawnPad?" | `inspect_instance` |
| "make a 5-checkpoint lava obby with respawn" | `execute_luau` (many parts + scripts, one call) |
| "playtest it and tell me the errors" | `start_stop_play` + `get_console_output` |
| "screenshot the viewport" | `screen_capture` |
| "generate a rock arch mesh" | `generate_mesh` / `generate_procedural_model` |
| "upload `C:\art\logo.png` as a decal" | `open_cloud_invoke` (or relay + API key) |
| "publish v3 with note 'obby'" | `open_cloud_invoke` (`universe-places:write`) |

---

## Troubleshooting

### `Manage MCP Servers` is missing
Update Studio (Help ▸ About, then restart and let it patch). The built‑in server only appears in
recent builds.

### Assistant panel won't open
`View ▸ Assistant`, or click the Assistant icon at the right of the top bar. If it still fails,
log out/in inside Studio once; the panel is tied to your session.

### Relay prints `could not reach Roblox Studio through either transport`
1. Is a **place open**? (Not the Start Page.)
2. Is **Enable Studio as MCP server** on? (Step 2)
3. First connection in a fresh Studio can take ~30 s while the MCP process starts — the relay
   retries for about 50 s, so let it finish.
4. Still failing: close Studio, re-open the place, restart the relay window.

### The green client count stays at 0
The MCP switch is off, or the toggle needs to be flipped off/on after Studio updates. Also check
the relay window for a `stdio launch failed` line and send it to me.

### `[FAIL] Cloud API auth` HTTP 401/403
Only affects the API-key fallback path. Fix the key at <https://create.roblox.com/credentials>
(scopes: `universe-place:read/write`, `asset:read/write`), replace the single line in
`%USERPROFILE%\ArenaRoblox\roblox_api_key.txt`, restart the relay. Or just use Step 3's
MCP Permissions instead and ignore the key entirely.

### Uploads fail with a permissions/scope error
Approve the file's folder under **MCP Permissions ▸ Upload folders**, or grant
**Publish & manage assets**. Files must be inside an approved folder — I cannot copy them in.

### It worked yesterday, nothing today
Re-open Studio with the place, start the relay again. Studio must stay open the whole time.

---

## What runs where

| Component | Location | Purpose |
|---|---|---|
| `bridge.py` | this sandbox, :8788 | relay link + HTTP API for me |
| `roblox_relay.ps1` | your PC, `%USERPROFILE%\ArenaRoblox` | connects your PC out to the sandbox |
| Studio MCP server | inside Roblox Studio | executes Luau, edits scripts, playtests |
| `roblox_api_key.txt` | your PC | fallback for Cloud API calls |

Status page: <https://8789-iy8w5dbt2nw4x9qqg772j.e2b.app/> — relay, Studio, tool count, live log.
