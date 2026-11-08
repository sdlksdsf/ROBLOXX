#!/usr/bin/env python3
"""
Roblox Studio Bridge (v3) — stdlib only, HTTP long-poll transport.

Topology
--------
  [Arena agent]  --HTTP-->  [this bridge :8788]  <--long-poll HTTP--  [user relay on Windows]
                                                          |
                                                          +--WebSocket--> ws://127.0.0.1:13469/proxy
                                                                          (StudioMCP.exe -> Studio plugin)

Why HTTP long-poll instead of WebSockets on the relay link:
  * the sandbox preview proxy is a plain HTTPS reverse proxy; plain POSTs always work
  * no asyncio/threading mixups, no external deps (stdlib only)
  * every hop is trivially debuggable with curl

Relay-facing endpoints (token required):
  POST /relay/hello?token=T          -> {relay_id, hello, server_time}
  POST /relay/poll?token=T&relay=R   -> {messages:[...]}   (holds up to POLL_HOLD s)
  POST /relay/send?token=T&relay=R   -> {ok:true}          body = message from StudioMCP

Agent-facing endpoints (same port, no token needed inside the sandbox):
  GET  /ping | /status | /tools | /logs
  POST /rpc {method,params}          raw MCP JSON-RPC
  POST /tools/call {name,arguments}
  POST /execute {code,datamodel_type}
  POST /api {url,method,headers,body}
  POST /cloud {path|url,method,body,api_key?}
"""
import json
import os
import queue
import secrets
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HOST = "0.0.0.0"
HTTP_PORT = int(os.environ.get("BRIDGE_HTTP_PORT", "8788"))
BRIDGE_TOKEN = os.environ.get("BRIDGE_TOKEN", secrets.token_hex(8))
MCP_PROTOCOL = "2024-11-05"
POLL_HOLD = float(os.environ.get("BRIDGE_POLL_HOLD", "15"))
PUBLIC_BASE = os.environ.get("BRIDGE_PUBLIC_BASE", "").rstrip("/")   # stable public URL, if known
RELAY_STALE = 120.0         # seconds without poll before a relay is considered dead
                            # (generous: the relay may be busy running a long API call)

LOG_LINES = deque(maxlen=800)
LOG_LOCK = threading.Lock()
VERBOSE = os.environ.get("BRIDGE_VERBOSE", "1") != "0"


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    if VERBOSE:
        print(line, flush=True)
    with LOG_LOCK:
        LOG_LINES.append(line)


# ---------------------------------------------------------------------------
# Relay connection object
# ---------------------------------------------------------------------------
class Relay:
    def __init__(self, rid):
        self.rid = rid
        self.cond = threading.Condition()
        self.outbox = deque()
        self.pending = {}            # id -> {"ev": Event, "resp": msg}
        self.closed = False
        self.connected_at = time.time()
        self.last_poll = 0.0
        self.last_seen = time.time()
        self.polls = 0
        self.tools = {}
        self.studio_id = None
        self.peer = "?"

    # -- outbound -----------------------------------------------------------
    def enqueue(self, msg, kind="rpc"):
        with self.cond:
            if self.closed:
                return False
            self.outbox.append((kind, msg))
            self.cond.notify_all()
            return True

    def poll(self, hold=POLL_HOLD):
        deadline = time.time() + hold
        with self.cond:
            while not self.outbox and not self.closed:
                left = deadline - time.time()
                if left <= 0:
                    break
                self.cond.wait(left)
            msgs = list(self.outbox)
            self.outbox.clear()
            self.last_poll = self.last_seen = time.time()
            self.polls += 1
            return msgs

    def close(self, why=""):
        with self.cond:
            self.closed = True
            self.cond.notify_all()
            pend = list(self.pending.values())
            self.pending.clear()
        for slot in pend:
            slot["resp"] = {"error": "relay closed %s" % why}
            slot["ev"].set()
        log("Relay %s closed (%s)" % (self.rid, why))

    # -- inbound ------------------------------------------------------------
    def deliver(self, msg):
        mid = msg.get("id")
        with self.cond:
            self.last_seen = time.time()
            slot = self.pending.pop(mid, None) if mid is not None else None
        if slot is None:
            return False
        slot["resp"] = msg
        slot["ev"].set()
        return True

    # -- request/response ---------------------------------------------------
    def request(self, msg, timeout=120, kind="rpc"):
        mid = msg.get("id") or new_id()
        msg["id"] = mid
        slot = {"ev": threading.Event(), "resp": None}
        with self.cond:
            if self.closed:
                return {"error": "relay not connected"}
            self.pending[mid] = slot
            self.outbox.append((kind, msg))
            self.cond.notify_all()
        if not slot["ev"].wait(timeout):
            with self.cond:
                self.pending.pop(mid, None)
            return {"error": "timeout", "id": mid, "method": msg.get("method")}
        return slot["resp"]

    def rpc(self, method, params, timeout=120):
        return self.request({"type": "json_rpc", "jsonrpc": "2.0", "id": new_id(),
                             "method": method, "params": params}, timeout)

    def api(self, url, method, headers, body, timeout=120):
        return self.request({"type": "api", "id": new_id(), "url": url,
                             "method": method, "headers": headers or {},
                             "body": body or ""}, timeout, kind="api")

    def snapshot(self):
        with self.cond:
            return {
                "relay_id": self.rid,
                "closed": self.closed,
                "polls": self.polls,
                "outbox": len(self.outbox),
                "pending": len(self.pending),
                "poll_age_sec": round(time.time() - self.last_poll, 1) if self.last_poll else None,
                "stale": (self.last_poll is None) or (time.time() - self.last_poll > RELAY_STALE),
                "age_sec": round(time.time() - self.connected_at, 1),
                "tools_count": len(self.tools),
                "studio_id": self.studio_id,
                "peer": getattr(self, "peer", "?"),
            }


RELAYS = {}
RELAYS_LOCK = threading.Lock()
ACTIVE = {"rid": None}
ID_LOCK = threading.Lock()
ID_SEQ = {"n": 0}


def new_id():
    with ID_LOCK:
        ID_SEQ["n"] += 1
        return "b%d" % ID_SEQ["n"]


def active_relay():
    rid = ACTIVE["rid"]
    if rid is None:
        return None
    r = RELAYS.get(rid)
    if r is None or r.closed:
        return None
    if r.last_poll and time.time() - r.last_poll > RELAY_STALE:
        return None
    return r


def require_relay():
    r = active_relay()
    if r is None:
        raise RuntimeError("relay not connected (no live relay polling the bridge)")
    return r


# ---------------------------------------------------------------------------
# MCP helpers
# ---------------------------------------------------------------------------
def tool_result_payload(resp):
    """Unwrap MCP tools/call result -> (dict|None, raw)"""
    if not isinstance(resp, dict):
        return None, resp
    if "error" in resp:
        return None, resp
    result = resp.get("result") or {}
    content = result.get("content") or []
    texts = []
    for c in content:
        if isinstance(c, dict) and c.get("type") == "text":
            texts.append(c.get("text", ""))
    joined = "\n".join(texts)
    try:
        return json.loads(joined), joined
    except Exception:
        return None, joined


def handshake(relay):
    """initialize -> notifications/initialized -> tools/list -> list_roblox_studios"""
    try:
        res = relay.rpc("initialize", {
            "protocolVersion": MCP_PROTOCOL,
            "capabilities": {"tools": {"listChanged": True}},
            "clientInfo": {"name": "arena-roblox-bridge", "version": "3.0"},
        }, timeout=25)
        info = (res.get("result") or {}).get("serverInfo") if isinstance(res, dict) else None
        log("initialize -> %s" % (str(info or res)[:160]))
        relay.enqueue({"type": "json_rpc", "jsonrpc": "2.0", "method": "notifications/initialized"})

        res = relay.rpc("tools/list", {}, timeout=40)
        tools = ((res.get("result") or {}).get("tools") or []) if isinstance(res, dict) else []
        for t in tools:
            relay.tools[t.get("name")] = t
        log("tools/list -> %d tools" % len(tools))

        res = relay.rpc("tools/call", {"name": "list_roblox_studios", "arguments": {}}, timeout=40)
        data, raw = tool_result_payload(res)
        studios = (data or {}).get("studios") or []
        if len(studios) == 1:
            relay.studio_id = studios[0].get("id")
        log("list_roblox_studios -> %d studio(s), bound=%s" % (len(studios), relay.studio_id))
        if not studios:
            log("  (open a place in Studio — the MCP server only exposes studios once a game is open)")
    except Exception as e:
        log("handshake error: %r" % (e,))


def call_tool(relay, name, arguments, timeout=300):
    args = dict(arguments or {})
    tool = relay.tools.get(name) or {}
    required = (tool.get("inputSchema") or {}).get("required") or []
    if "studio_id" in required and not args.get("studio_id") and relay.studio_id:
        args["studio_id"] = relay.studio_id
    elif name != "list_roblox_studios" and not args.get("studio_id") and relay.studio_id:
        args["studio_id"] = relay.studio_id
    return relay.rpc("tools/call", {"name": name, "arguments": args}, timeout=timeout)


DASHBOARD_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Arena <-> Roblox Studio</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0f1115;--card:#181b22;--line:#252a34;--txt:#e6e9ef;--dim:#98a2b3;--ok:#3ddc84;--bad:#ff5c5c;--acc:#6d8cff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font:14px/1.5 ui-sans-serif,system-ui,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:20px;margin:0 0 4px}.sub{color:var(--dim);margin-bottom:22px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px;margin-bottom:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.card h2{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim);margin:0 0 8px;font-weight:600}
.big{font-size:22px;font-weight:650}.ok{color:var(--ok)}.bad{color:var(--bad)}
pre{background:#0b0d11;border:1px solid var(--line);border-radius:10px;padding:14px;overflow:auto;max-height:320px;font-size:12px;color:#c8d0e0}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
input,button{font:inherit;border-radius:8px;border:1px solid var(--line);background:#11141a;color:var(--txt);padding:9px 12px}
input{flex:1;min-width:220px}button{background:var(--acc);border-color:var(--acc);color:#08101f;font-weight:600;cursor:pointer}
button.ghost{background:transparent;color:var(--txt);border-color:var(--line)}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:8px;background:var(--bad)}
.dot.on{background:var(--ok);box-shadow:0 0 10px var(--ok)}
</style></head><body><div class="wrap">
<h1>Arena &rarr; Roblox Studio</h1>
<div class="sub">Bridge, relay and Studio health. Keep this tab open while the agent works.</div>
<div class="grid">
  <div class="card"><h2>Relay</h2><div class="big"><span class="dot" id="d1"></span><span id="relay">checking...</span></div></div>
  <div class="card"><h2>Studio</h2><div class="big" id="studio">-</div><div class="sub" style="margin:4px 0 0" id="studiosub"></div></div>
  <div class="card"><h2>MCP tools</h2><div class="big" id="tools">-</div></div>
  <div class="card"><h2>Uptime</h2><div class="big" id="uptime">-</div></div>
</div>
<div class="card" style="margin-bottom:18px">
  <h2>Run Luau in the open place</h2>
  <div class="row">
    <input id="code" placeholder='print("hello"); return workspace:GetChildren()' value='print("hello from the dashboard")'>
    <button onclick="run()">Execute</button>
    <button class="ghost" onclick="check()">Refresh</button>
  </div>
  <pre id="out">ready.</pre>
</div>
<div class="card"><h2>Bridge log</h2><pre id="log">-</pre></div>
<script>
const $=id=>document.getElementById(id);
function fmt(s){return s<60?s+'s':s<3600?Math.floor(s/60)+'m':'h'}
async function check(){
  try{
    const st=await (await fetch('/status')).json();
    $('relay').textContent = st.connected ? 'connected' : 'not connected';
    $('d1').className = 'dot'+(st.connected?' on':'');
    $('studio').textContent = st.studio_id || '-';
    $('studio').className = 'big '+(st.studio_id?'ok':'bad');
    $('studiosub').textContent = st.studio_id ? '' : 'open a place in Studio';
    $('tools').textContent = st.tools_count || 0;
    $('uptime').textContent = fmt(st.uptime_sec||0);
    const h=await (await fetch('/health')).json();
    $('log').textContent=(h.log_tail||[]).join(String.fromCharCode(10))||'-';
  }catch(e){ $('relay').textContent='bridge unreachable'; $('d1').className='dot'; }
}
async function run(){
  $('out').textContent='running...';
  try{
    const r=await fetch('/execute',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({code:$('code').value,datamodel_type:'Edit'})});
    $('out').textContent=JSON.stringify(await r.json(),null,2);
  }catch(e){ $('out').textContent='error: '+e; }
}
check(); setInterval(check,4000);
</script></div></body></html>"""


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "RobloxBridge/3.0"

    def log_message(self, fmt, *a):
        pass

    # -- helpers --------------------------------------------------------
    def _send(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_html(self, code, html):
        data = html.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_relay_file(self, name):
        """Hand out the Windows relay bundle so setup is one paste."""
        name = os.path.basename(name or "")
        allow = {"roblox_relay.ps1", "setup.ps1", "roblox_api_key.txt", "README.md"}
        if name not in allow:
            return self._send(404, {"error": "unknown file", "allowed": sorted(allow)})
        # the credential is only handed out with the relay token
        if name == "roblox_api_key.txt" and not self._auth():
            return
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "relay", name)
        if not os.path.isfile(path):
            return self._send(404, {"error": "not found", "path": path})
        with open(path, "rb") as fh:
            data = fh.read()
        ctype = "text/plain; charset=utf-8"
        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {"_raw": raw.decode("utf-8", "replace")}

    def _q(self):
        return parse_qs(urlparse(self.path).query)

    @property
    def path_only(self):
        return urlparse(self.path).path

    def _auth(self):
        tok = (self._q().get("token") or [""])[0] or self.headers.get("X-Bridge-Token", "")
        if tok != BRIDGE_TOKEN:
            self._send(401, {"error": "bad token", "hint": "append ?token=<shared secret>"})
            return False
        return True

    # -- GET ------------------------------------------------------------
    def do_GET(self):
        p = self.path_only
        if p == "/ping":
            self._send(200, {"pong": True, "bridge": "roblox-studio-bridge", "v": 3,
                             "time": int(time.time())})
        elif p == "/status":
            with RELAYS_LOCK:
                rels = [r.snapshot() for r in RELAYS.values()]
            a = active_relay()
            now = time.time()
            self._send(200, {
                "connected": a is not None,
                "live": bool(a and a.last_poll and now - a.last_poll < 20),
                "active_relay": ACTIVE["rid"],
                "relays": rels,
                "studio_id": a.studio_id if a else None,
                "tools_count": len(a.tools) if a else 0,
                "waiting_requests": len(a.pending) if a else 0,
                "uptime_sec": int(time.time() - STARTED),
            })
        elif p == "/tools":
            a = active_relay()
            tools = a.tools if a else {}
            self._send(200, {"count": len(tools), "tools": {
                k: {"description": (v.get("description") or "")[:120],
                    "required": (v.get("inputSchema") or {}).get("required", [])}
                for k, v in tools.items()}})
        elif p == "/logs":
            with LOG_LOCK:
                self._send(200, {"logs": list(LOG_LINES)})
        elif p == "/":
            self._send_html(200, DASHBOARD_HTML)
        elif p == "/bootstrap":
            base = PUBLIC_BASE or ("http://%s:%d" % (self.headers.get("Host", "127.0.0.1"), HTTP_PORT))
            cmd = ('iwr -UseBasicParsing "%s/relay/file/setup.ps1?token=%s" -OutFile "$env:TEMP\arena.ps1"; '
                   '& "$env:TEMP\arena.ps1"') % (base, BRIDGE_TOKEN)
            self._send(200, {"public_base": base, "token": BRIDGE_TOKEN, "line": cmd})
        elif p == "/health":
            a = active_relay()
            with LOG_LOCK:
                tail = list(LOG_LINES)[-15:]
            self._send(200, {
                "ok": a is not None,
                "public_base": PUBLIC_BASE or None,
                "relay": a.rid if a else None,
                "studio_id": a.studio_id if a else None,
                "tools": len(a.tools) if a else 0,
                "log_tail": tail,
            })
        elif p.startswith("/relay/file/"):
            self._serve_relay_file(p[len("/relay/file/"):])
        elif p == "/relay/poll":          # tolerate GET for probing
            self._do_poll()
        else:
            self._send(404, {"error": "unknown path", "path": p})

    # -- POST -----------------------------------------------------------
    def do_POST(self):
        p = self.path_only
        if p in ("/relay/hello", "/relay/poll", "/relay/send"):
            if not self._auth():
                return
            if p == "/relay/hello":
                return self._do_hello()
            if p == "/relay/poll":
                return self._do_poll()
            return self._do_relay_send()
        body = self._body()
        try:
            if p == "/rpc":
                r = require_relay()
                self._send(200, r.rpc(body.get("method", ""), body.get("params", {}),
                                      timeout=body.get("timeout", 120)))
            elif p == "/tools/call":
                r = require_relay()
                self._send(200, call_tool(r, body.get("name", ""), body.get("arguments", {}),
                                          timeout=body.get("timeout", 300)))
            elif p == "/execute":
                r = require_relay()
                self._send(200, call_tool(r, "execute_luau", {
                    "code": body.get("code", ""),
                    "datamodel_type": body.get("datamodel_type", "Edit"),
                }, timeout=body.get("timeout", 300)))
            elif p == "/api":
                r = require_relay()
                self._send(200, r.api(body.get("url", ""), body.get("method", "GET"),
                                      body.get("headers", {}), body.get("body", ""),
                                      timeout=body.get("timeout", 120)))
            elif p == "/cloud":
                r = require_relay()
                path = body.get("path") or body.get("url") or ""
                url = path if path.startswith("http") else "https://apis.roblox.com" + path
                headers = dict(body.get("headers") or {})
                # The relay injects the credential file that sits next to it
                # (x-api-key or Bearer). Only fall back to the bridge-side token
                # when the caller explicitly asks for it.
                if body.get("api_key"):
                    headers["x-api-key"] = body["api_key"]
                elif body.get("use_bridge_token") and BEARER_TOKEN:
                    headers.setdefault("Authorization", "Bearer " + BEARER_TOKEN)
                elif body.get("bearer"):
                    headers.setdefault("Authorization", "Bearer " + body["bearer"])
                self._send(200, r.api(url, body.get("method", "GET"), headers,
                                      body.get("body", ""), timeout=body.get("timeout", 120)))
            else:
                self._send(404, {"error": "unknown path", "path": p})
        except RuntimeError as e:
            self._send(503, {"error": str(e)})
        except Exception as e:
            log("HTTP %s failed: %r" % (p, e))
            self._send(500, {"error": repr(e)})

    # -- relay endpoints ------------------------------------------------
    def _do_hello(self):
        rid = "relay-%d-%s" % (int(time.time()), secrets.token_hex(3))
        r = Relay(rid)
        try:
            r.peer = "%s:%s" % (self.client_address[0], self.client_address[1])
            fwd = self.headers.get("X-Forwarded-For")
            if fwd:
                r.peer = fwd.split(",")[0].strip() + " (via proxy)"
        except Exception:
            r.peer = "?"
        with RELAYS_LOCK:
            for old in list(RELAYS.values()):
                if not old.closed:
                    log("closing previous relay %s (%s)" % (old.rid, getattr(old, "peer", "?")))
                    old.close("replaced by newer relay")
            # forget relay sessions that have been dead for a while
            for dead in [k for k, v in RELAYS.items() if v.closed and time.time() - v.connected_at > 300]:
                RELAYS.pop(dead, None)
            RELAYS[rid] = r
        ACTIVE["rid"] = rid
        log("Relay connected: %s from %s" % (rid, self.client_address[0]))
        self._send(200, {"ok": True, "relay_id": rid, "server_time": int(time.time()),
                         "hello": {"type": "hello", "bridge": "roblox-studio-bridge", "v": 3},
                         "poll_hold_sec": POLL_HOLD})
        threading.Thread(target=handshake, args=(r,), daemon=True).start()

    def _do_poll(self):
        rid = (self._q().get("relay") or [""])[0]
        r = RELAYS.get(rid)
        if r is None or r.closed:
            return self._send(409, {"error": "unknown relay", "relay_id": ACTIVE["rid"],
                                    "rejoin": True})
        try:
            hold = float((self._q().get("hold") or [POLL_HOLD])[0])
        except ValueError:
            hold = POLL_HOLD
        msgs = r.poll(min(max(hold, 0.0), 25.0))
        # "raw" carries the exact JSON text; the relay forwards rpc payloads
        # verbatim, so Luau source is never re-serialized on the Windows side.
        out = [{"kind": k, "raw": json.dumps(m, separators=(",", ":"))} for k, m in msgs]
        self._send(200, {"messages": out, "n": len(out), "server_time": int(time.time())})

    def _do_relay_send(self):
        rid = (self._q().get("relay") or [""])[0]
        r = RELAYS.get(rid)
        if r is None or r.closed:
            return self._send(409, {"error": "unknown relay", "rejoin": True})
        msg = self._body()
        if not isinstance(msg, dict):
            return self._send(400, {"error": "message must be a JSON object"})
        if isinstance(msg.get("messages"), list):     # batch form
            ok = sum(1 for m in msg["messages"] if isinstance(m, dict) and r.deliver(m))
            return self._send(200, {"ok": True, "delivered": ok})
        ok = r.deliver(msg)
        self._send(200, {"ok": True, "delivered": ok})


# ---------------------------------------------------------------------------
# Bearer token (Cloud API calls are executed by the relay, on the user's box)
# ---------------------------------------------------------------------------
BEARER_TOKEN = os.environ.get("ROBLOX_TOKEN", "")
STARTED = time.time()


def load_token():
    global BEARER_TOKEN
    if BEARER_TOKEN:
        return
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.environ.get("ROBLOX_TOKEN_FILE"),
              os.path.join(here, "relay", "roblox_api_key.txt"),
              os.path.join(here, "relay", "roblox_token.txt")):
        if p and os.path.exists(p):
            val = open(p).read().strip()
            if val:
                BEARER_TOKEN = val
                log("loaded Roblox credential from %s (%d chars)" % (p, len(val)))
                return
    log("WARNING: no Roblox credential found — /cloud calls will be unauthenticated")


def monitor():
    while True:
        time.sleep(10)
        a = active_relay()
        if a is None and ACTIVE["rid"]:
            log("no live relay polling (active=%s)" % ACTIVE["rid"])


def main():
    load_token()
    srv = ThreadingHTTPServer((HOST, HTTP_PORT), Handler)
    srv.daemon_threads = True
    log("bridge v3 listening on http://%s:%d" % (HOST, HTTP_PORT))
    log("relay token: %s" % BRIDGE_TOKEN)
    threading.Thread(target=monitor, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
