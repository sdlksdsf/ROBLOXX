# Setup guide — Arena agent ⇄ your Roblox Studio

Follow this top to bottom. Total time: about 5 minutes.
Everything runs on **your Windows PC**; nothing needs a router change or port forwarding.

---

## Step 0 — What you need

| Requirement | Check |
|---|---|
| Windows 10/11 | — |
| Roblox Studio installed and up to date | open Studio once, let it update |
| A place **open** in Studio | File ▸ Open a saved place (or New Baseplate and save it) |
| This Arena chat open | the sandbox bridge must be alive |

> The sandbox (this agent) cannot reach your computer. Your PC connects **out** to the
> sandbox and stays connected. That is the whole trick.

---

## Step 1 — Turn on local ports in Studio (once)

1. Open Roblox Studio with a place loaded.
2. Menu: **File ▸ Studio Settings**.
3. Tab **Security**.
4. Tick **Allow local ports to access Studio**.
5. Close the settings window.

Without this, Studio refuses the local connection and every tool call hangs.

---

## Step 2 — Open PowerShell

Press `Win`, type `powershell`, press Enter. A blue window appears.

*(Windows Terminal or PowerShell 7 work too.)*

---

## Step 3 — Paste this one line and press Enter

```powershell
iwr -UseBasicParsing "https://8788-iy8w5dbt2nw4x9qqg772j.e2b.app/relay/file/setup.ps1" -OutFile "$env:TEMP\arena.ps1"; & "$env:TEMP\arena.ps1"
```

What it does, in order:

| It does | You see |
|---|---|
| checks the bridge is alive | `bridge : OK (v3)` |
| creates `%USERPROFILE%\ArenaRoblox` | `install dir : C:\Users\you\ArenaRoblox` |
| downloads the relay + your API key | `downloaded : roblox_relay.ps1 (32595 bytes)` |
| runs a preflight check | a PASS/FAIL table |
| opens the relay window | `READY - waiting for orders` |

---

## Step 4 — Read the preflight table

You should see green lines like this:

```
  PREFLIGHT REPORT
  ----------------
  [PASS] PowerShell               v5.1.x
  [PASS] Roblox Studio running    pid 12345
  [PASS] mcp.bat present          C:\Users\you\AppData\Local\Roblox\mcp.bat
  [PASS] StudioMCP.exe present    ...\Versions\version-xxxx\StudioMCP.exe
  [PASS] port 13469 listening     Studio MCP socket is up
  [PASS] bridge reachable         HTTP 200 -> https://8788-....e2b.app
  [PASS] Roblox API key file      ...\ArenaRoblox\roblox_api_key.txt
  [PASS] Cloud API auth           HTTP 404 (401/403 means the key is invalid)
```

If something is red, jump to **Troubleshooting** below — each line tells you what to fix.

---

## Step 5 — Wait for READY

The relay window prints a numbered handshake:

```
[08:12:04] [OK]   bridge reachable: HTTP 200
[08:12:04] [OK]   studio transport: websocket ws://127.0.0.1:13469/proxy
[08:12:05] [OK]   joined bridge as relay-1794123456-ab12cd
[08:12:05] [OK]   READY - waiting for orders from the Arena agent (Ctrl+C to stop)
```

**Leave this window open.** Closing it disconnects us. Minimising is fine.

If instead you see `studio transport: stdio ... mcp.bat`, that is also correct — the relay
fell back to Roblox's official MCP process. Both work.

---

## Step 6 — Confirm in chat

Say exactly: **connected**

The agent then checks the bridge and runs a live probe:

```
python3 roblox.py code "print('connected')"
```

You will see in Studio's **Output** window (`View ▸ Output`):

```
connected
```

That proves the loop end to end. From then on, just describe what you want built.

---

## Step 7 — Work normally

Speak plainly. Examples that map onto real tool calls:

| You say | What happens |
|---|---|
| "build a 20×20 spawn platform at 0,0,0 with a red neon ring" | parts created in Workspace |
| "add a ServerScript that gives every player 100 coins and a leaderstat" | script written into ServerScriptService |
| "read the script at ServerScriptService.Main and fix the bug" | source read, patched, written back |
| "make a lava obby, 5 checkpoints, respawn to last checkpoint" | many ops in one round trip |
| "upload `C:\art\logo.png` as a decal and put it on a billboard" | Cloud API upload from your PC |
| "publish this place and note 'v3 — obby'" | Open Cloud publish |
| "playtest it and tell me if the script errors" | Play-Solo + error report |

Keep the relay window open for the whole session. To stop, press `Ctrl+C` in it.

---

## Keeping it connected later

After the first install, you do **not** need the one-liner again. Either:

* **Shortcut**: right-click `%USERPROFILE%\ArenaRoblox\roblox_relay.ps1` ▸ *Run with PowerShell*.
* **Command** (adapt the URL if the session changed):

```powershell
powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\ArenaRoblox\roblox_relay.ps1" `
  -BridgeUrl "https://8788-iy8w5dbt2nw4x9qqg772j.e2b.app" `
  -Token "bridge-secret-iy8w5dbt2nw4x9qqg772j"
```

Add `-Trace` for verbose logging, `-Preflight` to re-run diagnostics only,
`-Transport ws` or `-Transport stdio` to force one path.

---

## Troubleshooting

### `bridge : UNREACHABLE`
The sandbox bridge is not running (session asleep/ended). Ask the agent to check it, then re-run
Step 3. Nothing on your side is broken.

### `[FAIL] Roblox Studio running`
Studio is closed. Open it, load a place, then press `Ctrl+C` in the relay window and start the
relay again.

### `[FAIL] mcp.bat present` or `StudioMCP.exe present`
Your Studio build is older or incomplete. In Studio: **Help ▸ About** verify the version, then
let Studio update (Roblox updates it in the background). The relay's fallback needs that file;
the websocket path can still work without it if port 13469 is up.

### `[FAIL] port 13469 listening` and the relay loops on `connect attempt`
1. Is a **place open**? A blank "Start Page" is not enough.
2. Is **Allow local ports to access Studio** ticked? (Step 1)
3. First tool call sometimes needs the plugin warm — the relay retries for ~50 s, that is normal.
4. Still stuck: tick **File ▸ Studio Settings ▸ Security** again, restart Studio, retry.

### `could not reach Roblox Studio through either transport`
Same as above. Also check antivirus isn't blocking `powershell.exe` opening `cmd.exe`/`StudioMCP.exe`.

### `[FAIL] Cloud API auth` with `HTTP 401` / `HTTP 403`
The Roblox API key is invalid, expired, or lacks scopes. Fix at
<https://create.roblox.com/credentials>: create a key with the scopes you need
(`universe-place:read/write`, `asset:read/write`, etc.), then replace the one line inside
`%USERPROFILE%\ArenaRoblox\roblox_api_key.txt` and restart the relay.

### Relay says `READY` but the agent still sees nothing
The agent's `connected: False` means no relay has reached the bridge. Check that the relay's
bridge URL matches the one the agent prints, and that your network allows HTTPS to `*.e2b.app`
(some corporate proxies block it — try a phone hotspot to confirm).

### Works, then drops after your PC sleeps
Just start the relay again (Step "Keeping it connected later"). Studio must also still have the
place open.

---

## What is running where

| Component | Location | Purpose |
|---|---|---|
| `bridge.py` | this sandbox, port 8788 | relay link + HTTP API for the agent |
| `roblox_relay.ps1` | your PC, `%USERPROFILE%\ArenaRoblox` | bridges your PC to the sandbox |
| Studio MCP | your PC, `127.0.0.1:13469` or `mcp.bat` | executes Luau inside your open place |
| `roblox_api_key.txt` | your PC | Open Cloud auth (upload / publish) |

Status page: <https://8789-iy8w5dbt2nw4x9qqg772j.e2b.app/> (relay, Studio, tools, live log).
