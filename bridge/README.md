# Arena ⇄ Roblox Studio

Ask in chat, watch it happen inside the place you have open in Roblox Studio.

```
Arena agent  ──HTTPS──▶  bridge.py (this sandbox, port 8788)
                              ▲
                              │  HTTPS long-poll (outbound only — no ports opened on your PC)
                              ▼
                   roblox_relay.ps1  (your Windows PC)
                              │  WebSocket 127.0.0.1:13469  *or*  stdio  mcp.bat
                              ▼
                     Roblox Studio  +  Roblox Open Cloud (asset upload / publish)
```

Everything on your machine is **outbound only**: the relay calls the bridge, never the other
way around. That means no firewall changes, no port forwarding, and it works on hotel/office
Wi-Fi.

---

## 1. Start the relay (one line, PowerShell)

```powershell
iwr -UseBasicParsing "https://8788-iy8w5dbt2nw4x9qqg772j.e2b.app/relay/file/setup.ps1" -OutFile "$env:TEMP\arena.ps1"; & "$env:TEMP\arena.ps1"
```

The installer drops everything in `%USERPROFILE%\ArenaRoblox` (relay script + your Roblox API
key), checks it can reach the bridge, then opens a relay window. Leave that window open.

**Step-by-step guide: [`SETUP.md`](SETUP.md).** Status page: <https://8789-iy8w5dbt2nw4x9qqg772j.e2b.app/> (or `:8788/`) — shows relay, studio,
tool count, live log, and a box to run Luau yourself. Machine-readable health: `/health`.

## 2. Studio prerequisites (once)

| Thing | Where |
|---|---|
| Place open | a saved place must be loaded — MCP only exposes studios once a game is open |
| Local ports | `File ▸ Studio Settings ▸ Security ▸ Allow local ports to access Studio` = **on** |
| MCP | the assistant/MCP plugin shipped with Studio must be enabled; the relay can also launch Roblox's own `%LOCALAPPDATA%\Roblox\mcp.bat` (stdio fallback) |

The relay tries the `ws://127.0.0.1:13469/proxy` socket first (fastest) and falls back to
`mcp.bat` automatically, so either Studio build works. Force one with `-Transport ws` or
`-Transport stdio`.

## 3. Then just talk

> make a red 20×1×20 spawn pad at 0,0,0 with a PointLight, add a ServerScript that teleports
> players onto it, then playtest it

The agent executes it through `roblox.py` (see below) and reports real Studio output.

---

## What the agent drives

`bridge/roblox.py` — client + CLI:

```bash
python3 roblox.py doctor                      # bridge → relay → studio → luau health
python3 roblox.py code "print('hi')"          # run Luau in Edit mode
python3 roblox.py code "..." --play           # run in Play-Solo
python3 roblox.py create workspace.Pad Part --props '{"Size":[10,1,10],"Anchored":true}'
python3 roblox.py set workspace.Pad Color '[1,0,0]'
python3 roblox.py script ServerScriptService.Main --file main.server.luau
python3 roblox.py read ServerScriptService.Main
python3 roblox.py tree workspace --depth 2
python3 roblox.py apply --plan plan.json      # many ops in ONE round trip
python3 roblox.py upload C:\art\logo.png --type Decal --creator-id 1414009
python3 roblox.py publish --place 123456 --type Published --universe 987654
python3 roblox.py cloud /cloud/v2/universes/987654/places --method GET
```

Library form:

```python
from roblox import Studio
s = Studio()                       # http://127.0.0.1:8788 in the sandbox
s.luau("print('hello')")           # -> {'ok': True, 'logs': [...], 'result': ...}
s.create("workspace.SpawnPad", "Part", {"Size": [10, 1, 10], "Anchored": True})
s.upload_asset(r"D:\art\logo.png", creator_id="1414009", asset_type="Decal")
```

### Why responses are reliable

Luau is wrapped before it runs: `print` is captured, errors are caught with `pcall`, and the
return value is JSON-encoded and printed with an `__ARENA_RESULT__` marker. The bridge and relay
never re-serialise the payload (they forward the exact JSON text), and the client brace-matches
the marker — so logs, return values and errors survive no matter how the MCP server formats its
output. Typical round trip in-sandbox: **~17 ms**.

## Bridge endpoints

| Endpoint | Purpose |
|---|---|
| `GET /` | dashboard |
| `GET /health` | compact health for scripts |
| `GET /ping`, `/status`, `/tools`, `/logs`, `/health` | introspection |
| `POST /execute` | `{code, datamodel_type}` → `tools/call execute_luau` |
| `POST /tools/call` | `{name, arguments}` — any MCP tool |
| `POST /rpc` | raw JSON-RPC |
| `POST /api` | `{url, method, headers, body}` executed by the relay (reaches the internet) |
| `POST /cloud` | Roblox Open Cloud path, auto-prefixed with `apis.roblox.com` |
| `POST /relay/hello`, `/relay/poll`, `/relay/send` | relay link (token-gated, 15 s long-poll) |
| `GET /relay/file/<name>` | hands the Windows bundle to your PC |

## How to tell it is really connected

`/status` is the source of truth:

| Field | Meaning |
|---|---|
| `connected` | a relay is joined and not stale |
| `live` | that relay polled within the last 20 s (i.e. the window is actually running) |
| `relays[].peer` | the public address the relay connected from |
| `studio_id` | the Studio instance the bridge bound to |
| `tools_count` | tools the MCP server advertised |

If `relays` is empty, nothing from your PC has reached this bridge yet — nothing is connected.

## Verified behaviour

* 17 ms per Luau round trip in-sandbox; 12 concurrent calls all succeed
* errors, `print` output and return values survive the wrapper (checked against a real
  Lua 5.5 interpreter with a mocked `game:GetService("HttpService"):JSONEncode`)
* every generated Luau snippet parses (`luaparser`), including unicode and quoted instance names
* the relay never re-serialises JSON: Studio's raw message is the HTTP body, so Luau source
  reaches Studio byte-for-byte

## Credentials

`bridge/relay/roblox_api_key.txt` holds the Roblox Open Cloud key (prefix `Rn227…`). The relay
reads it and injects `x-api-key` on `roblox.com` calls; the bridge serves it only to callers
holding the relay token. Both credential files and logs are git-ignored — rotate the key in the
[Creator Dashboard](https://create.roblox.com/credentials) if it ever leaks.
