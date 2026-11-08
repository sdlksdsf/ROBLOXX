#!/usr/bin/env python3
"""
roblox.py - high level client for the Arena <-> Roblox Studio bridge.

Everything here talks to bridge.py (default http://127.0.0.1:8788), which in turn
talks to your Windows relay (roblox_relay.ps1), which talks to Roblox Studio's own
MCP server. So any call from here lands inside the place you have open.

Library
    from roblox import Studio
    s = Studio()
    s.status()
    s.luau("print('hello from the agent')")          -> {'ok':True,'logs':[...]}
    s.create("workspace.SpawnPad", "Part", {"Size": [10,1,10], "Anchored": True})
    s.set("workspace.SpawnPad", "Color", [1,0,0])
    s.script("ServerScriptService.Main", source, kind="Script")
    s.tree("workspace", depth=2)
    s.publish(place_id=123456, version_type="Published")   # Roblox Open Cloud, via your machine

CLI
    python3 roblox.py status
    python3 roblox.py tools
    python3 roblox.py code "print('hi')"          # or: --file script.luau
    python3 roblox.py create workspace.Pad Part --props '{"Size":[10,1,10]}'
    python3 roblox.py set workspace.Pad Anchored true
    python3 roblox.py script ServerScriptService.Main --file main.server.luau
    python3 roblox.py tree workspace --depth 2
    python3 roblox.py play start | play stop
    python3 roblox.py cloud /cloud/v2/universes/123/places --method GET
    python3 roblox.py publish --place 123456 --type Published --notes "v1"
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BASE = os.environ.get("BRIDGE_URL", "http://127.0.0.1:8788")
DEFAULT_TOKEN = os.environ.get("BRIDGE_TOKEN", "bridge-secret-iy8w5dbt2nw4x9qqg772j")
MARKER = "__ARENA_RESULT__"


def lua_str(value) -> str:
    """Render a Python string as a Luau string literal.

    json.dumps escapes quotes, backslashes and newlines the way Lua wants, but
    it also emits uXXXX escapes for non-ASCII text, which Luau cannot parse -
    hence ensure_ascii=False here.
    """
    return json.dumps(value, ensure_ascii=False)


# --------------------------------------------------------------------------- #
#  Luau helpers
# --------------------------------------------------------------------------- #
def wrap_luau(code: str, patch_print: bool = True) -> str:
    """Wrap user code so prints + errors + a return value come back as JSON.

    Works in both Edit and Play-Solo datamodels.  The result is emitted on stdout
    with a unique marker so it can be recovered no matter how the MCP server
    formats its text response.
    """
    if not patch_print:
        # Play-Solo: stubbing print would swallow the game's own output, so only
        # capture the optional return value and any error.
        return f"""
local __arena_ok, __arena_ret = pcall(function()
{_indent(code, 4)}
end)
local function __arena_esc(v)
    if v == nil then return "null" end
    local ok, s = pcall(function() return game:GetService("HttpService"):JSONEncode(v) end)
    if ok then return s end
    return game:GetService("HttpService"):JSONEncode(tostring(v))
end
local __arena_err = __arena_ok and "null" or __arena_esc(__arena_ret)
local __arena_res = __arena_ok and __arena_esc(__arena_ret) or "null"
local __arena_json = '{{"__arena":{{"ok":' .. tostring(__arena_ok) ..
    ',"error":' .. __arena_err .. ',"logs":[],"result":' .. __arena_res .. '}}}}'
print("{MARKER}" .. __arena_json)
""".strip()

    return f"""
local __arena_logs = {{}}
local __arena_oldprint = print
local function __arena_str(v)
    if type(v) == "table" then
        local ok, s = pcall(function() return game:GetService("HttpService"):JSONEncode(v) end)
        if ok then return s end
        return tostring(v)
    end
    return tostring(v)
end
print = function(...)
    local parts = {{}}
    for i = 1, select("#", ...) do
        parts[#parts + 1] = __arena_str((select(i, ...)))
    end
    __arena_logs[#__arena_logs + 1] = table.concat(parts, "\\t")
    __arena_oldprint(...)
end
local __arena_ok, __arena_ret = pcall(function()
{_indent(code, 4)}
end)
print = __arena_oldprint
local __arena_payload = {{
    ok = __arena_ok,
    error = (not __arena_ok) and tostring(__arena_ret) or nil,
    logs = __arena_logs,
    result = __arena_ok and __arena_ret or nil,
}}
local __arena_json
local __arena_jsonok, __arena_j = pcall(function()
    return game:GetService("HttpService"):JSONEncode({{ __arena = __arena_payload }})
end)
if __arena_jsonok then __arena_json = __arena_j else __arena_json = '{{"__arena":{{"ok":true}}}}' end
print("{MARKER}" .. __arena_json)
""".strip()


def _indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line if line.strip() else line for line in text.splitlines())


def luau_quote(value) -> str:
    """Render a Python value as a Luau literal."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "nil"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return lua_str(value)          # JSON strings are valid Luau strings
    if isinstance(value, (list, tuple)):
        return "{" + ", ".join(luau_quote(v) for v in value) + "}"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k} = {luau_quote(v)}" for k, v in value.items()) + "}"
    return luau_quote(str(value))


def path_expr(path: str) -> str:
    """'workspace.Pad/Hinge' -> Luau expression.  Also accepts 'Workspace:Pad'."""
    path = path.strip().lstrip("./")
    if path.lower().startswith("game."):
        parts = path[5:].replace(":", ".").replace("/", ".").split(".")
    else:
        parts = path.replace(":", ".").replace("/", ".").split(".")
    parts = [p for p in parts if p]
    if not parts:
        raise ValueError("empty instance path")
    expr = f'game:GetService("{parts[0]}")' if parts[0] in SERVICES else f'game:GetService("Workspace")'
    for p in parts[1:] if parts[0] in SERVICES else parts:
        expr += f':FindFirstChild({lua_str(p)}, true)'
    return expr


SERVICES = {
    "Workspace", "Players", "Lighting", "ReplicatedStorage", "ReplicatedFirst",
    "ServerScriptService", "ServerStorage", "StarterGui", "StarterPack",
    "StarterPlayer", "SoundService", "Chat", "Teams", "TextChatService",
    "MaterialService", "PhysicsService", "PathfindingService", "TweenService",
    "HttpService", "DataStoreService", "MarketplaceService", "TeleportService",
    "CollectionService", "RunService", "UserInputService", "ContextActionService",
}


# --------------------------------------------------------------------------- #
#  Client
# --------------------------------------------------------------------------- #
class BridgeError(RuntimeError):
    pass


class Studio:
    def __init__(self, base: str = DEFAULT_BASE, token: str = DEFAULT_TOKEN, timeout: int = 180):
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.studio_id = None
        self._last_mcp_error = None

    # ---------------------------------------------------------------- plumbing
    def _post(self, path: str, payload: dict, timeout: int | None = None) -> dict:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self.base}{path}?token={self.token}", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:600]
            raise BridgeError(f"HTTP {exc.code} from {path}: {detail}") from None
        except urllib.error.URLError as exc:
            raise BridgeError(
                f"cannot reach the bridge at {self.base} ({exc.reason}). "
                "Is bridge.py running and is the relay connected?") from None

    def _get(self, path: str, timeout: int = 20) -> dict:
        url = f"{self.base}{path}"
        sep = "&" if "?" in path else "?"
        req = urllib.request.Request(f"{url}{sep}token={self.token}", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        return self._get("/status")

    def require_relay(self) -> dict:
        st = self.status()
        if not st.get("connected"):
            raise BridgeError(
                "no Roblox Studio relay is connected.\n"
                "  on your PC:  powershell -ExecutionPolicy Bypass -File roblox_relay.ps1\n"
                "  and make sure Roblox Studio is open with a place loaded.")
        self.studio_id = st.get("studio_id")
        return st

    def tools(self) -> dict:
        return self._get("/tools")

    # --------------------------------------------------------------- raw shots
    def call_tool(self, name: str, arguments: dict | None = None, timeout: int | None = None) -> dict:
        return self._post("/tools/call", {"name": name, "arguments": arguments or {},
                                          "timeout": timeout or self.timeout})

    def rpc(self, method: str, params: dict | None = None, timeout: int | None = None) -> dict:
        return self._post("/rpc", {"method": method, "params": params or {},
                                                "timeout": timeout or self.timeout})

    # Studio accepts Edit | Server | Client (Server/Client only during a playtest)
    _DM = {"edit": "Edit", "server": "Server", "client": "Client", "play": "Server"}

    def run_raw(self, code: str, datamodel_type: str = "Edit", timeout: int | None = None) -> str:
        """Send raw Luau, return the MCP text payload untouched."""
        dm = self._DM.get(str(datamodel_type).lower(), "Edit")
        r = self._post("/execute", {"code": code, "datamodel_type": dm,
                                    "timeout": timeout or self.timeout})
        return _content_text(r)

    # --------------------------------------------------------------------- Luau
    def luau(self, code: str, datamodel_type: str = "Edit", timeout: int | None = None) -> dict:
        """Run Luau in Studio and return {'ok', 'logs', 'error', 'result', 'raw'}."""
        self.require_relay()
        play = str(datamodel_type).lower() in ("play", "server", "client")
        text = self.run_raw(wrap_luau(code, patch_print=not play), datamodel_type, timeout)
        return _parse_result(text)

    # -------------------------------------------------------------- scene edits
    def create(self, path: str, class_name: str, props: dict | None = None,
               parent: str | None = None) -> dict:
        props = props or {}
        parent_expr = path_expr(parent) if parent else f"({path_expr(path)}).Parent"
        prop_lua = "".join(f"\n__arena_o.{k} = {luau_quote(v)}" for k, v in props.items())
        code = f"""
local __arena_p = {parent_expr}
if not __arena_p then error("parent not found") end
local __arena_o = Instance.new("{class_name}")
__arena_o.Name = {lua_str(path.split('.')[-1].split('/')[-1])}{prop_lua}
__arena_o.Parent = __arena_p
return __arena_o:GetFullName()
"""
        return self.luau(code)

    def set(self, path: str, prop: str, value) -> dict:
        code = f"""
local __arena_o = {path_expr(path)}
if not __arena_o then error("not found: " .. {lua_str(path)}) end
__arena_o.{prop} = {luau_quote(value)}
return {{ name = __arena_o:GetFullName(), {prop} = __arena_o.{prop} }}
"""
        return self.luau(code)

    def get(self, path: str, prop: str) -> dict:
        code = f"""
local __arena_o = {path_expr(path)}
if not __arena_o then error("not found: " .. {lua_str(path)}) end
return __arena_o.{prop}
"""
        return self.luau(code)

    def destroy(self, path: str) -> dict:
        return self.luau(f"""
local __arena_o = {path_expr(path)}
if __arena_o then __arena_o:Destroy() return "destroyed " .. {lua_str(path)} end
return "not found"
""")

    def children(self, path: str = "workspace") -> dict:
        return self.luau(f"""
local __arena_o = {path_expr(path)}
if not __arena_o then error("not found: " .. {lua_str(path)}) end
local __arena_t = {{}}
for _, c in ipairs(__arena_o:GetChildren()) do
    __arena_t[#__arena_t + 1] = {{ name = c.Name, class = c.ClassName }}
end
return __arena_t
""")

    def tree(self, path: str = "game", depth: int = 2) -> dict:
        return self.luau(f"""
local __arena_root = game
if {lua_str(path)} ~= "game" then __arena_root = {path_expr(path)} end
if not __arena_root then error("not found") end
local function walk(node, d)
    if d > {int(depth)} then return nil end
    local out = {{ name = node.Name, class = node.ClassName }}
    if d < {int(depth)} then
        local kids = {{}}
        for _, c in ipairs(node:GetChildren()) do
            local sub = walk(c, d + 1)
            if sub then kids[#kids + 1] = sub end
        end
        out.children = kids
    end
    return out
end
return walk(__arena_root, 1)
""")

    def script(self, path: str, source: str, kind: str = "Script", parent: str | None = None) -> dict:
        """Create-or-update a script with the given source."""
        target_parent = parent or ".".join(path.split(".")[:-1])
        name = path.split(".")[-1].split("/")[-1]
        parent_expr = path_expr(target_parent) if target_parent else path_expr("ServerScriptService")
        code = f"""
local __arena_parent = {parent_expr}
if not __arena_parent then error("parent not found: " .. {lua_str(target_parent)}) end
local __arena_s = __arena_parent:FindFirstChild({lua_str(name)}, true)
if __arena_s and not __arena_s:IsA("LuaSourceContainer") then
    error("an object named " .. {lua_str(name)} .. " already exists and is a " .. __arena_s.ClassName)
end
if not __arena_s then
    __arena_s = Instance.new("{kind}")
    __arena_s.Name = {lua_str(name)}
    __arena_s.Parent = __arena_parent
end
__arena_s.Source = {lua_str(source)}
return {{ path = __arena_s:GetFullName(), class = __arena_s.ClassName, bytes = #__arena_s.Source }}
"""
        return self.luau(code)

    def script_read(self, path: str) -> dict:
        """Prefer the dedicated MCP tool, fall back to Luau."""
        try:
            r = self.call_tool("script_read", {"target_file": path})
            text = _content_text(r)
            return {"ok": True, "source": _maybe_json(text) or text, "raw": text}
        except BridgeError:
            code = f"""
local __arena_o = {path_expr(path)}
if not __arena_o then error("not found: " .. {lua_str(path)}) end
return __arena_o.Source
"""
            return self.luau(code)

    # -------------------------------------------------------------- inspection
    def studio_state(self) -> dict:
        """Play state and the datamodel types Studio currently allows."""
        try:
            return {"ok": True, "result": _parse_result(
                _content_text(self.call_tool("get_studio_state", {}, timeout=30)))}
        except BridgeError as exc:
            return {"ok": False, "error": str(exc)}

    def console(self, lines: int = 40) -> dict:
        """Tail Studio's Output log - the fastest way to see real errors."""
        try:
            text = _content_text(self.call_tool("get_console_output", {}, timeout=60))
            return {"ok": True, "lines": text.splitlines()[-lines:], "raw": text}
        except BridgeError as exc:
            return {"ok": False, "error": str(exc), "lines": []}

    def search_scripts(self, query: str) -> dict:
        """Fuzzy search scripts by name (script_search)."""
        try:
            return {"ok": True, "result": _parse_result(
                _content_text(self.call_tool("script_search", {"query": query}, timeout=60)))}
        except BridgeError as exc:
            return {"ok": False, "error": str(exc)}

    def grep_scripts(self, pattern: str) -> dict:
        try:
            return {"ok": True, "result": _parse_result(
                _content_text(self.call_tool("script_grep", {"pattern": pattern}, timeout=90)))}
        except BridgeError as exc:
            return {"ok": False, "error": str(exc)}

    def inspect(self, path: str) -> dict:
        """Properties, attributes and children of one instance."""
        try:
            return {"ok": True, "result": _parse_result(
                _content_text(self.call_tool("inspect_instance", {"path": path}, timeout=60)))}
        except BridgeError as exc:
            return {"ok": False, "error": str(exc)}

    def screenshot(self) -> dict:
        """Capture the Studio viewport."""
        try:
            res = self.call_tool("screen_capture", {}, timeout=120)
            return {"ok": True, "raw": _content_text(res)[:4000]}
        except BridgeError as exc:
            return {"ok": False, "error": str(exc)}

    # ------------------------------------------------------------------ playtest
    def play(self, action: str = "start") -> dict:
        """Start/stop Play-Solo through start_stop_play (Edit datamodel)."""
        want = action != "stop"
        last = None
        for args in ({"datamodel_type": "Edit", "is_start": want},
                     {"datamodel_type": "Edit", "start": want},
                     {"is_start": want}):
            try:
                res = self.call_tool("start_stop_play", args, timeout=90)
                text = _content_text(res)
                if "Method not found" not in text:
                    return {"ok": True, "args": args, "response": text[:600]}
                last = text
            except BridgeError as exc:
                last = str(exc)
        return {"ok": False, "error": last or "start_stop_play unavailable",
                "hint": "press F5 / Shift+F5 in Studio instead"}

    # -------------------------------------------------------------- cloud calls
    def cloud(self, path: str, method: str = "GET", body: dict | None = None,
              api_key: str | None = None, timeout: int | None = None,
              via: str = "auto", scopes: list | None = None,
              files: list | None = None) -> dict:
        """Roblox Open Cloud request.

        via="mcp"   -> open_cloud_invoke inside Studio: no API key, uses the MCP
                       Permissions you granted (place must be a published game)
        via="relay" -> plain HTTPS from your PC using the API key file
        via="auto"  -> MCP first, relay as fallback
        """
        if via in ("mcp", "auto"):
            args = {"method": method.upper(), "endpoint": path}
            if body is not None:
                args["body"] = body if isinstance(body, str) else json.dumps(body)
            if scopes:
                args["scopes"] = scopes
            if files:
                args["files"] = files
            try:
                res = self.call_tool("open_cloud_invoke", args, timeout=timeout or 180)
                out = _parse_result(_content_text(res))
                if out.get("ok"):
                    return {"via": "mcp", "body": out.get("result")
                            if out.get("result") is not None else out.get("logs")}
                self._last_mcp_error = out.get("error")
                if via == "mcp":
                    return {"via": "mcp", "body": {"error": out.get("error")}}
            except BridgeError as exc:
                self._last_mcp_error = str(exc)
                if via == "mcp":
                    return {"via": "mcp", "body": {"error": str(exc)}}

        payload = {"path": path, "method": method}
        if body is not None:
            payload["body"] = body if isinstance(body, str) else json.dumps(body)
        if api_key:
            payload["api_key"] = api_key
        r = self._post("/cloud", payload, timeout or 180)
        raw = r.get("body", "")
        parsed = _maybe_json(raw)
        return {"via": "relay", "status": r.get("status"),
                "body": parsed if parsed is not None else raw}

    def publish(self, place_id: int, version_type: str = "Published",
                notes: str = "", universe_id: int | None = None) -> dict:
        """Publish the open place with the Open Cloud Places API."""
        body = {"versionType": version_type}
        if notes:
            body["description"] = notes
        r = self.cloud(f"/cloud/v2/universes/{universe_id}/places/{place_id}/versions",
                       "POST", body) if universe_id else \
            self.cloud(f"/universes/{place_id}/place", "POST", body)
        return r

    # -------------------------------------------------------------- asset upload
    def upload_asset(self, file_path: str, name: str | None = None,
                     description: str = "", asset_type: str = "Decal",
                     creator_id: str | None = None, creator_kind: str = "user") -> dict:
        """Upload a file as a Roblox asset from YOUR machine (the relay reads it).

        file_path is a Windows path. The relay streams the bytes to
        apis.roblox.com/assets/v1/assets with multipart/form-data.
        asset_type: Decal | Audio | Model | Mesh  (Image is uploaded as Decal)
        """
        display = name or os.path.basename(file_path)
        request = {"assetType": asset_type, "displayName": display,
                   "description": description}
        if creator_id:
            key = "groupId" if creator_kind == "group" else "userId"
            request["creationContext"] = {"creator": {key: str(creator_id)}}

        # preferred: Studio's own MCP upload (uses MCP Permissions + upload folders)
        try:
            res = self.call_tool("open_cloud_invoke", {
                "method": "POST", "endpoint": "/assets/v1/assets",
                "body": json.dumps(request), "files": [file_path],
                "scopes": ["asset:write"]}, timeout=600)
            out = _parse_result(_content_text(res))
            if out.get("ok"):
                return {"via": "mcp", "file": file_path,
                        "body": out.get("result") if out.get("result") is not None else out.get("logs"),
                        "hint": "file must sit in a folder approved under MCP Permissions > Upload folders"}
        except BridgeError:
            pass

        payload = {"file": file_path, "assetType": asset_type, "name": display,
                   "description": description, "creatorKind": creator_kind}
        if creator_id:
            payload["creatorId"] = str(creator_id)
        r = self._post("/cloud", {"path": "/assets/v1/assets", "method": "UPLOAD",
                                  "upload": payload}, timeout=600)
        raw = r.get("body", "")
        parsed = _maybe_json(raw)
        return {"via": "relay", "status": r.get("status"),
                "body": parsed if parsed is not None else raw}

    def asset_operation(self, operation_id: str) -> dict:
        """Poll an async asset upload until the asset id is ready."""
        for _ in range(60):
            r = self.cloud(f"/assets/v1/operations/{operation_id}", "GET")
            body = r.get("body") or {}
            if isinstance(body, dict) and body.get("done"):
                return r
            time.sleep(2)
        return r

    # ------------------------------------------------------- place file download
    def download_place(self, place_id: int, out_path: str, universe_id: int | None = None) -> dict:
        """Save a copy of a cloud place file onto your machine (relay writes it)."""
        r = self._post("/cloud", {
            "path": f"/cloud/v2/universes/{universe_id}/places/{place_id}/versions:download"
                    if universe_id else f"/universes/{place_id}/place",
            "method": "DOWNLOAD", "download": {"out": out_path}}, timeout=600)
        return r

    # -------------------------------------------------------------- diagnostics
    def doctor(self) -> dict:
        """One-shot health check of every hop."""
        report = {"bridge": None, "relay": None, "studio": None, "luau": None}
        try:
            report["bridge"] = self._get("/ping")
        except Exception as exc:
            report["bridge"] = {"error": str(exc)}
            return report
        st = self.status()
        report["relay"] = {"connected": st.get("connected"),
                           "relay": st.get("active_relay"),
                           "studio_id": st.get("studio_id"),
                           "tools": st.get("tools_count")}
        if not st.get("connected"):
            return report
        try:
            names = list((self.tools().get("tools") or {}).keys())
            report["studio"] = {"tool_count": len(names),
                                "has_execute_luau": "execute_luau" in names,
                                "has_open_cloud": "open_cloud_invoke" in names,
                                "has_play": "start_stop_play" in names,
                                "studio_id": self.studio_id,
                                "tools": names}
        except Exception as exc:
            report["studio"] = {"error": str(exc)}
        try:
            res = self.luau('return { luau = _VERSION or "?", children = #workspace:GetChildren() }')
            report["luau"] = {"ok": res.get("ok"), "result": res.get("result"),
                              "error": res.get("error")}
        except Exception as exc:
            report["luau"] = {"error": str(exc)}
        try:
            report["console_tail"] = (self.console(6).get("lines") or [])[-6:]
        except Exception:
            pass
        return report

    # ------------------------------------------------------------- bulk helpers
    def apply(self, plan: list[dict]) -> list[dict]:
        """Run several operations (from a JSON plan) inside one Luau round trip."""
        chunks, out = [], []
        for i, op in enumerate(plan):
            kind = op.get("op", "create")
            if kind == "create":
                parent = op.get("parent") or ".".join(op["path"].split(".")[:-1])
                name = op["path"].split(".")[-1]
                props = "".join(f"\n    o.{k} = {luau_quote(v)}" for k, v in (op.get("props") or {}).items())
                chunks.append(f"""
do
  local p = {path_expr(parent)}
  if p then
    local o = Instance.new("{op.get('class','Part')}")
    o.Name = {lua_str(name)}{props}
    o.Parent = p
    table.insert(RESULTS, {{ ok = true, op = {lua_str(kind)}, path = {lua_str(op['path'])} }})
  else
    table.insert(RESULTS, {{ ok = false, op = {lua_str(kind)}, path = {lua_str(op['path'])}, error = "parent missing" }})
  end
end""")
            elif kind == "set":
                chunks.append(f"""
do
  local o = {path_expr(op['path'])}
  if o then o.{op['prop']} = {luau_quote(op.get('value'))}; table.insert(RESULTS, {{ ok = true, op = "set", path = {lua_str(op['path'])} }})
  else table.insert(RESULTS, {{ ok = false, op = "set", path = {lua_str(op['path'])}, error = "missing" }}) end
end""")
            elif kind == "destroy":
                chunks.append(f"""
do
  local o = {path_expr(op['path'])}
  if o then o:Destroy() end
  table.insert(RESULTS, {{ ok = true, op = "destroy", path = {lua_str(op['path'])} }})
end""")
            elif kind == "script":
                parent = op.get("parent") or ".".join(op["path"].split(".")[:-1])
                name = op["path"].split(".")[-1]
                chunks.append(f"""
do
  local p = {path_expr(parent)}
  if p then
    local s = p:FindFirstChild({lua_str(name)}, true)
    if not s then s = Instance.new("{op.get('kind','Script')}"); s.Name = {lua_str(name)}; s.Parent = p end
    s.Source = {lua_str(op.get('source',''))}
    table.insert(RESULTS, {{ ok = true, op = "script", path = {lua_str(op['path'])} }})
  else
    table.insert(RESULTS, {{ ok = false, op = "script", path = {lua_str(op['path'])}, error = "parent missing" }})
  end
end""")
            else:
                out.append({"ok": False, "op": kind, "error": "unknown op"})
        code = "local RESULTS = {}\n" + "\n".join(chunks) + "\nreturn { results = RESULTS }"
        res = self.luau(code)
        results = None
        if isinstance(res.get("result"), dict):
            results = res["result"].get("results")
        if results is None:
            # the server returned something unexpected: surface it instead of []
            out.append({"ok": bool(res.get("ok")), "op": "batch",
                        "error": res.get("error"), "raw": res.get("raw")})
            return out
        return list(results) + out


# --------------------------------------------------------------------------- #
#  Response parsing
# --------------------------------------------------------------------------- #
def _content_text(rpc_response: dict) -> str:
    if not isinstance(rpc_response, dict):
        return str(rpc_response)
    if "error" in rpc_response:
        return json.dumps(rpc_response["error"])
    result = rpc_response.get("result", rpc_response)
    if isinstance(result, str):
        return result
    for block in (result.get("content") or []) if isinstance(result, dict) else []:
        if block.get("type") == "text":
            return block.get("text", "")
    return json.dumps(result)


def _maybe_json(text: str):
    if not isinstance(text, str):
        return None
    t = text.strip()
    if t[:1] in "[{" and t[-1:] in "]}":
        try:
            return json.loads(t)
        except json.JSONDecodeError:
            return None
    return None


def _extract_json_object(text: str, start: int):
    """Brace-match a JSON object starting at the first '{' at/after `start`."""
    i = text.find("{", start)
    if i < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for j in range(i, len(text)):
        ch = text[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[i:j + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _try_unescape(text: str):
    """MCP servers sometimes hand back JSON-escaped text; undo one layer."""
    try:
        out = json.loads('"' + text + '"')
        return out if isinstance(out, str) else None
    except json.JSONDecodeError:
        return None


def _arena_payload(text: str):
    if not isinstance(text, str):
        return None
    raw = text.strip()
    if raw[:1] == '"' and raw[-1:] == '"':
        raw = raw[1:-1]
    variants = [text,
                _try_unescape(text),
                _try_unescape(raw),
                raw.replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\')]
    for variant in variants:
        if not variant:
            continue
        idx = variant.find(MARKER)
        if idx < 0:
            continue
        obj = _extract_json_object(variant, idx + len(MARKER))
        if isinstance(obj, dict):
            return obj.get("__arena", obj)
    return None


ERROR_PREFIX = re.compile(r'\[string "[^"]*"\]:?\s*')


def _clean_error(msg):
    """'[string "local __arena_..."]:3: boom' -> '3: boom'"""
    if not isinstance(msg, str):
        return msg
    return ERROR_PREFIX.sub("", msg.strip()).strip() or msg.strip()


def _parse_result(text: str) -> dict:
    """Normalise whatever the MCP server returned into a dict."""
    out = {"ok": True, "logs": [], "error": None, "result": None, "raw": text}
    payload = _arena_payload(text)
    if isinstance(payload, dict):
        out.update(ok=bool(payload.get("ok", True)),
                   logs=payload.get("logs") or [],
                   error=_clean_error(payload.get("error")),
                   result=payload.get("result"))
        return out
    parsed = _maybe_json(text)
    if isinstance(parsed, dict) and ("ok" in parsed or "logs" in parsed):
        out.update(ok=bool(parsed.get("ok", True)), logs=parsed.get("logs") or [],
                   error=parsed.get("error"), result=parsed.get("result"))
        return out
    low = text.lower()
    if "compile_error" in low or "syntax error" in low or "failed to compile" in low:
        out["ok"] = False
        out["error"] = _clean_error(text)[:2000]
        out["result"] = None
        return out
    if "error" in low and ("traceback" in low or "attempt to" in low or "error:" in low):
        out["ok"] = False
        out["error"] = _clean_error(text)[:2000]
    out["result"] = text.strip()
    return out


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="roblox.py", description="Talk to Roblox Studio through the bridge")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--token", default=DEFAULT_TOKEN)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")
    sub.add_parser("tools")

    p = sub.add_parser("code", help="run Luau")
    p.add_argument("code", nargs="?")
    p.add_argument("--file")
    p.add_argument("--play", action="store_true", help="run in Play-Solo datamodel")

    p = sub.add_parser("create")
    p.add_argument("path")
    p.add_argument("class_name")
    p.add_argument("--props", default="{}")
    p.add_argument("--parent")

    p = sub.add_parser("set")
    p.add_argument("path")
    p.add_argument("prop")
    p.add_argument("value", help="JSON or literal")

    p = sub.add_parser("get")
    p.add_argument("path")
    p.add_argument("prop")

    p = sub.add_parser("script")
    p.add_argument("path")
    p.add_argument("--file", required=True)
    p.add_argument("--kind", default="Script")

    p = sub.add_parser("read")
    p.add_argument("path")

    p = sub.add_parser("tree")
    p.add_argument("path", nargs="?", default="game")
    p.add_argument("--depth", type=int, default=2)

    p = sub.add_parser("children")
    p.add_argument("path", nargs="?", default="workspace")

    p = sub.add_parser("delete")
    p.add_argument("path")

    p = sub.add_parser("play")
    p.add_argument("action", choices=["start", "stop"])

    p = sub.add_parser("cloud")
    p.add_argument("path")
    p.add_argument("--method", default="GET")
    p.add_argument("--body")
    p.add_argument("--api-key")

    p = sub.add_parser("publish")
    p.add_argument("--place", type=int, required=True)
    p.add_argument("--type", default="Published")
    p.add_argument("--notes", default="")
    p.add_argument("--universe", type=int)

    p = sub.add_parser("apply", help="run a JSON plan in one round trip")
    p.add_argument("--plan", required=True, help="path to a JSON file, or inline JSON")

    p = sub.add_parser("upload", help="upload an asset from your PC via the relay")
    p.add_argument("file", help="Windows path, e.g. C:\\assets\\logo.png")
    p.add_argument("--name")
    p.add_argument("--type", default="Decal", choices=["Decal", "Audio", "Model", "Mesh"])
    p.add_argument("--description", default="")
    p.add_argument("--creator-id")
    p.add_argument("--creator-kind", default="user", choices=["user", "group"])

    p = sub.add_parser("doctor", help="check bridge -> relay -> studio -> luau")
    sub.add_parser("state", help="Studio play state + datamodel types")
    p = sub.add_parser("console", help="tail Studio's output log")
    p.add_argument("--lines", type=int, default=40)
    p = sub.add_parser("search", help="find scripts by name")
    p.add_argument("query")
    p = sub.add_parser("grep", help="search all scripts for a pattern")
    p.add_argument("pattern")
    p = sub.add_parser("inspect", help="inspect one instance")
    p.add_argument("path")
    sub.add_parser("screenshot", help="capture the Studio viewport")

    args = ap.parse_args(argv)
    s = Studio(args.base, args.token)

    try:
        if args.cmd == "status":
            print(json.dumps(s.status(), indent=2))
        elif args.cmd == "tools":
            print(json.dumps(s.tools(), indent=2))
        elif args.cmd == "code":
            src = open(args.file, encoding="utf-8").read() if args.file else args.code
            if not src:
                ap.error("provide code or --file")
            _print_result(s.luau(src, "Play" if args.play else "Edit"))
        elif args.cmd == "create":
            _print_result(s.create(args.path, args.class_name, json.loads(args.props), args.parent))
        elif args.cmd == "set":
            _print_result(s.set(args.path, args.prop, _literal(args.value)))
        elif args.cmd == "get":
            _print_result(s.get(args.path, args.prop))
        elif args.cmd == "script":
            _print_result(s.script(args.path, open(args.file, encoding="utf-8").read(), args.kind))
        elif args.cmd == "read":
            _print_result(s.script_read(args.path))
        elif args.cmd == "tree":
            _print_result(s.tree(args.path, args.depth))
        elif args.cmd == "children":
            _print_result(s.children(args.path))
        elif args.cmd == "delete":
            _print_result(s.destroy(args.path))
        elif args.cmd == "play":
            print(json.dumps(s.play(args.action), indent=2))
        elif args.cmd == "cloud":
            print(json.dumps(s.cloud(args.path, args.method,
                                     json.loads(args.body) if args.body else None,
                                     args.api_key), indent=2))
        elif args.cmd == "publish":
            print(json.dumps(s.publish(args.place, args.type, args.notes, args.universe), indent=2))
        elif args.cmd == "upload":
            print(json.dumps(s.upload_asset(args.file, args.name, args.description,
                                            args.type, args.creator_id, args.creator_kind), indent=2))
        elif args.cmd == "state":
            print(json.dumps(s.studio_state(), indent=2)[:4000])
        elif args.cmd == "console":
            res = s.console(args.lines)
            print("\n".join(res.get("lines") or []) or res.get("error"))
        elif args.cmd == "search":
            print(json.dumps(s.search_scripts(args.query), indent=2)[:4000])
        elif args.cmd == "grep":
            print(json.dumps(s.grep_scripts(args.pattern), indent=2)[:4000])
        elif args.cmd == "inspect":
            print(json.dumps(s.inspect(args.path), indent=2)[:6000])
        elif args.cmd == "screenshot":
            print(json.dumps(s.screenshot(), indent=2)[:3000])
        elif args.cmd == "doctor":
            rep = s.doctor()
            print(json.dumps(rep, indent=2))
            if not (rep.get("luau") or {}).get("ok"):
                return 1
        elif args.cmd == "apply":
            plan = args.plan
            if os.path.exists(plan):
                plan = open(plan, encoding="utf-8").read()
            ops = json.loads(plan)
            print(json.dumps(s.apply(ops), indent=2))
    except BridgeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


def _literal(raw: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        if raw.lower() in ("true", "false"):
            return raw.lower() == "true"
        if raw.lower() == "nil":
            return None
        return raw


def _print_result(res: dict) -> None:
    ok = res.get("ok", True)
    for line in res.get("logs") or []:
        print(f"  [studio] {line}")
    if res.get("error"):
        print(f"  [error] {res['error']}")
    if res.get("result") not in (None, ""):
        val = res["result"]
        print(json.dumps(val, indent=2) if isinstance(val, (dict, list)) else f"  {val}")
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    raise SystemExit(main())
