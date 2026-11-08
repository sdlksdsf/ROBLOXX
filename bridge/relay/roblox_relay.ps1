<#
  roblox_relay.ps1  -  Roblox Studio  <->  Arena bridge relay (v4)
  ----------------------------------------------------------------
  Connects this PC to the Arena bridge, and drives Roblox Studio through
  Studio's own MCP server. Two transports are supported and tried in order:

    ws    : ws://127.0.0.1:13469/proxy    (the socket Studio's MCP plugin
            exposes - lowest latency, no extra process)
    stdio : %LOCALAPPDATA%\Roblox\mcp.bat (Roblox's official StudioMCP.exe,
            newline-delimited JSON-RPC over stdin/stdout)

  It also executes Roblox Open Cloud calls from this PC (the sandbox cannot
  reach apis.roblox.com): REST calls, multipart asset uploads, and place
  file downloads. The Roblox API key is read from roblox_api_key.txt that
  sits next to this script.

  Usage
    powershell -ExecutionPolicy Bypass -File .\roblox_relay.ps1
    powershell -ExecutionPolicy Bypass -File .\roblox_relay.ps1 -BridgeUrl "https://xxxx.e2b.app" -Token "xxxx" -Transport ws

  Once-only setup in Studio
    File > Studio Settings > Security > "Allow local ports to access Studio" = ON
    A place must be OPEN in Studio (MCP only exposes studios once a game is open)
#>
[CmdletBinding()]
param(
    [string]$BridgeUrl = "https://8788-iy8w5dbt2nw4x9qqg772j.e2b.app",
    [string]$Token     = "bridge-secret-iy8w5dbt2nw4x9qqg772j",
    [string]$ProxyUrl  = "ws://127.0.0.1:13469/proxy",
    [ValidateSet('auto', 'ws', 'stdio')]
    [string]$Transport = 'auto',
    [int]   $PollHold  = 15,
    [switch]$NoLaunchMcp,
    [switch]$Trace,
    [switch]$Preflight      # run diagnostics, print a report, then exit
)

$ErrorActionPreference = 'Continue'
try { [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12 } catch { }
try { [System.Net.ServicePointManager]::DefaultConnectionLimit = 16 } catch { }
Add-Type -AssemblyName System.Net.Http | Out-Null

$script:Base        = $BridgeUrl.TrimEnd('/')
$script:Rid         = $null
$script:Ws          = $null
$script:Proc        = $null
$script:Mode        = $null          # 'ws' | 'stdio'
$script:RecvTask    = $null
$script:Buf         = New-Object byte[] 262144
$script:Stat        = @{ toStudio = 0; fromStudio = 0; apiCalls = 0; errors = 0 }
$script:LogFile     = Join-Path $PSScriptRoot 'relay.log'
$script:Creds       = $null
$script:ProxyUri    = $ProxyUrl
$script:Stderr      = New-Object System.Text.StringBuilder

# ------------------------------------------------------------------ logging
function Log([string]$msg, [string]$level = 'INFO') {
    $line = '[{0}] [{1}] {2}' -f (Get-Date -Format 'HH:mm:ss'), $level, $msg
    switch ($level) {
        'ERR'   { Write-Host $line -ForegroundColor Red }
        'WARN'  { Write-Host $line -ForegroundColor Yellow }
        'OK'    { Write-Host $line -ForegroundColor Green }
        'TRACE' { if ($Trace) { Write-Host $line -ForegroundColor DarkGray } }
        default { Write-Host $line }
    }
    try { Add-Content -LiteralPath $script:LogFile -Value $line -ErrorAction SilentlyContinue } catch { }
}

# ------------------------------------------------------------- Roblox creds
function Load-Creds {
    if ($script:Creds) { return $script:Creds }
    $best = $null
    foreach ($name in @('roblox_api_key.txt', 'roblox_token.txt')) {
        $p = Join-Path $PSScriptRoot $name
        if (Test-Path -LiteralPath $p) {
            $v = (Get-Content -LiteralPath $p -Raw)
            if ($v) { $v = $v.Trim() }
            if ($v) {
                if ($name -eq 'roblox_api_key.txt') { $best = @{ kind = 'api_key'; value = $v }; break }
                if (-not $best) { $best = @{ kind = 'bearer'; value = $v } }
            }
        }
    }
    if ($best) { Log ('credential loaded: {0} ({1} chars)' -f $best.kind, $best.value.Length) 'OK' }
    else { Log 'no roblox_api_key.txt next to this script - Cloud calls will be unauthenticated' 'WARN' }
    $script:Creds = $best
    return $best
}

# -------------------------------------------------------------------- HTTP
$script:Http = [System.Net.Http.HttpClient]::new()
$script:Http.Timeout = [TimeSpan]::FromSeconds(360)
[void]$script:Http.DefaultRequestHeaders.TryAddWithoutValidation('User-Agent', 'arena-roblox-relay/4.0')

function New-JsonContent([string]$json) {
    [System.Net.Http.StringContent]::new($json, [System.Text.Encoding]::UTF8, 'application/json')
}

function Get-BridgeUri([string]$path) {
    $sep = if ($path -like '*?*') { '&' } else { '?' }
    $uri = '{0}{1}{2}token={3}' -f $script:Base, $path, $sep, [uri]::EscapeDataString($Token)
    if ($script:Rid) { $uri += '&relay=' + [uri]::EscapeDataString($script:Rid) }
    return $uri
}

function Post-Bridge([string]$path, [string]$json) {
    $content = New-JsonContent ($(if ($json) { $json } else { '{}' }))
    $resp = $script:Http.PostAsync((Get-BridgeUri $path), $content).Result
    $body = $resp.Content.ReadAsStringAsync().Result
    return [pscustomobject]@{ Status = [int]$resp.StatusCode; Body = $body }
}

function Test-LocalPort([int]$port = 13469, [int]$timeoutMs = 600) {
    $c = $null
    try {
        $c = [System.Net.Sockets.TcpClient]::new()
        $t = $c.ConnectAsync('127.0.0.1', $port)
        if ($t.Wait($timeoutMs) -and $c.Connected) { return $true }
        return $false
    } catch { return $false }
    finally { if ($c) { try { $c.Close() } catch { } } }
}

# ===========================================================================
#  Transport 1: WebSocket straight into Studio's MCP plugin (port 13469)
# ===========================================================================
function Connect-StudioWs([int]$timeoutMs = 4000) {
    if (-not (Test-LocalPort 13469)) { return $false }
    foreach ($url in @($script:ProxyUri, 'ws://127.0.0.1:13469/proxy', 'ws://127.0.0.1:13469/studio')) {
        try {
            $ws = [System.Net.WebSockets.ClientWebSocket]::new()
            $cts = [System.Threading.CancellationTokenSource]::new()
            $cts.CancelAfter($timeoutMs)
            $ws.ConnectAsync([Uri]$url, $cts.Token).Wait()
            if ($ws.State -eq [System.Net.WebSockets.WebSocketState]::Open) {
                $script:Ws = $ws
                $script:Mode = 'ws'
                $script:ProxyUri = $url
                Log "studio transport: websocket $url" 'OK'
                return $true
            }
        } catch {
            $ex = $_.Exception
            while ($ex.InnerException) { $ex = $ex.InnerException }
            Log ("ws {0} failed: {1}" -f $url, $ex.Message) 'TRACE'
        }
    }
    return $false
}

function Read-WsMessage {
    $res = $script:RecvTask.Result
    if ($res.MessageType -eq [System.Net.WebSockets.WebSocketMessageType]::Close) { return $null }
    $sb = [System.Text.StringBuilder]::new()
    [void]$sb.Append([System.Text.Encoding]::UTF8.GetString($script:Buf, 0, $res.Count))
    while (-not $res.EndOfMessage) {
        $seg = New-Object System.ArraySegment[byte] -ArgumentList @(, $script:Buf)
        $t = $script:Ws.ReceiveAsync($seg, [System.Threading.CancellationToken]::None)
        if (-not $t.Wait(20000)) { return $null }
        $res = $t.Result
        if ($res.MessageType -eq [System.Net.WebSockets.WebSocketMessageType]::Close) { return $null }
        [void]$sb.Append([System.Text.Encoding]::UTF8.GetString($script:Buf, 0, $res.Count))
    }
    return $sb.ToString()
}

# ===========================================================================
#  Transport 2: Roblox's official StudioMCP.exe over stdio (mcp.bat)
# ===========================================================================
function Connect-StudioStdio {
    if ($script:Proc -and -not $script:Proc.HasExited) { return $true }
    $bat = Join-Path (Join-Path $env:LOCALAPPDATA 'Roblox') 'mcp.bat'
    if (-not (Test-Path -LiteralPath $bat)) {
        Log "mcp.bat not found at $bat - cannot use the stdio transport" 'WARN'
        return $false
    }
    try {
        $psi = [System.Diagnostics.ProcessStartInfo]::new()
        $psi.FileName = 'cmd.exe'
        $psi.Arguments = '/c "' + $bat + '"'
        $psi.WorkingDirectory = Split-Path -Parent $bat
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.RedirectStandardInput = $true
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
        $psi.StandardInputEncoding = [System.Text.Encoding]::UTF8
        $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8

        $p = [System.Diagnostics.Process]::new()
        $p.StartInfo = $psi
        [void]$p.Start()
        $script:Stderr = New-Object System.Text.StringBuilder
        $handler = [System.Diagnostics.DataReceivedEventHandler]{
            param($sender, $e)
            if ($e.Data) { [void]$script:Stderr.AppendLine($e.Data) }
        }
        $p.add_ErrorDataReceived($handler)
        $p.BeginErrorReadLine()
        $script:Proc = $p
        $script:Mode = 'stdio'
        Log ("studio transport: stdio {0} (pid {1})" -f $bat, $p.Id) 'OK'
        return $true
    } catch {
        $ex = $_.Exception
        while ($ex.InnerException) { $ex = $ex.InnerException }
        Log ("stdio launch failed: {0}" -f $ex.Message) 'ERR'
        return $false
    }
}

function Read-StdioMessage {
    # RecvTask is a ReadLineAsync() task; returns one JSON line, or $null at EOF
    for ($i = 0; $i -lt 40; $i++) {
        $line = $script:RecvTask.Result
        if ($null -eq $line) { return $null }
        $t = $line.Trim()
        if ($t -and $t.StartsWith('{')) { return $t }
        if ($t) { Log ("studio stdio noise: {0}" -f $t.Substring(0, [Math]::Min(120, $t.Length))) 'TRACE' }
        $script:RecvTask = $script:Proc.StandardOutput.ReadLineAsync()
        if (-not $script:RecvTask.Wait(2000)) { return $null }
    }
    return $null
}

# ===========================================================================
#  Transport-agnostic connect / receive / send
# ===========================================================================
function Connect-Studio {
    $order = switch ($Transport) {
        'ws'    { @('ws') }
        'stdio' { @('stdio') }
        default { @('ws', 'stdio') }
    }
    for ($attempt = 1; $attempt -le 40; $attempt++) {
        foreach ($mode in $order) {
            if ($mode -eq 'ws') {
                if (Connect-StudioWs) { Start-Receive; return $true }
            } else {
                if ($NoLaunchMcp) { continue }
                if (Connect-StudioStdio) {
                    Start-Sleep -Milliseconds 300
                    Start-Receive
                    # a stdio MCP session needs its own initialize before tools work
                    if (Initialize-StudioStdio) { return $true }
                }
            }
        }
        if ($attempt -eq 4) {
            Log 'still waiting for Studio - open a place, and enable Settings > Security > "Allow local ports to access Studio"' 'WARN'
        }
        Start-Sleep -Milliseconds 1200
    }
    Log 'could not reach Roblox Studio through either transport' 'ERR'
    return $false
}

function Initialize-StudioStdio {
    # stdio transport: this process is our own MCP client, so handshake here
    try {
        Send-StudioText '{"jsonrpc":"2.0","id":"relay-init","method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"clientInfo":{"name":"arena-roblox-relay","version":"4.0"}}}'
        $deadline = (Get-Date).AddSeconds(15)
        while ((Get-Date) -lt $deadline) {
            if (-not $script:RecvTask.Wait(500)) { continue }
            $line = Read-StdioMessage
            if (-not $line) { return $false }
            if ($line -match '"id"\s*:\s*"relay-init"') {
                Log 'studio stdio handshake complete' 'OK'
                Send-StudioText '{"jsonrpc":"2.0","method":"notifications/initialized"}'
                return $true
            }
        }
        Log 'studio stdio handshake timed out' 'WARN'
        return $false
    } catch {
        Log "studio stdio handshake failed: $($_.Exception.Message)" 'WARN'
        return $false
    }
}

function Start-Receive {
    if ($script:Mode -eq 'ws') {
        $seg = New-Object System.ArraySegment[byte] -ArgumentList @(, $script:Buf)
        $script:RecvTask = $script:Ws.ReceiveAsync($seg, [System.Threading.CancellationToken]::None)
    } elseif ($script:Mode -eq 'stdio') {
        $script:RecvTask = $script:Proc.StandardOutput.ReadLineAsync()
    }
}

function Read-StudioMessage {
    if ($script:Mode -eq 'ws') { return Read-WsMessage }
    if ($script:Mode -eq 'stdio') { return Read-StdioMessage }
    return $null
}

function Send-StudioText([string]$text) {
    if ($script:Mode -eq 'ws') {
        if (-not $script:Ws -or $script:Ws.State -ne [System.Net.WebSockets.WebSocketState]::Open) { throw 'studio websocket is not open' }
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($text)
        $seg = New-Object System.ArraySegment[byte] -ArgumentList @(, $bytes)
        $t = $script:Ws.SendAsync($seg, [System.Net.WebSockets.WebSocketMessageType]::Text, $true,
                                  [System.Threading.CancellationToken]::None)
        if (-not $t.Wait(30000)) { throw 'send timed out' }
        return
    }
    if ($script:Mode -eq 'stdio') {
        if (-not $script:Proc -or $script:Proc.HasExited) { throw 'studio stdio process is not running' }
        $script:Proc.StandardInput.WriteLine($text)
        $script:Proc.StandardInput.Flush()
        return
    }
    throw 'no studio transport'
}

function Send-Studio([string]$text) {
    try {
        Send-StudioText $text
        $script:Stat.toStudio++
        return $true
    } catch {
        Log "send to Studio failed: $($_.Exception.Message)" 'ERR'
        return $false
    }
}

function Close-Studio {
    try { if ($script:Ws) { $script:Ws.Dispose() } } catch { }
    try {
        if ($script:Proc -and -not $script:Proc.HasExited) {
            # mcp.bat spawns StudioMCP.exe as a child: kill the tree
            $pid_ = $script:Proc.Id
            Start-Process -FilePath 'taskkill.exe' -ArgumentList '/F', '/T', '/PID', $pid_ -WindowStyle Hidden -Wait -ErrorAction SilentlyContinue | Out-Null
            $script:Proc.Kill()
        }
    } catch { }
    $script:Ws = $null
    $script:Proc = $null
    $script:Mode = $null
    $script:RecvTask = $null
}

function Test-StudioAlive {
    if ($script:Mode -eq 'ws') { return ($script:Ws -and $script:Ws.State -eq [System.Net.WebSockets.WebSocketState]::Open) }
    if ($script:Mode -eq 'stdio') { return ($script:Proc -and -not $script:Proc.HasExited) }
    return $false
}

# ===========================================================================
#  Roblox Open Cloud, executed from this PC
# ===========================================================================
# entries: @{ Id; Task; Url; Method; Mode; Out }
$script:ApiPending = New-Object System.Collections.ArrayList

function Get-AuthRequest([string]$url, [string]$method, [string]$contentType = 'application/json') {
    $req = [System.Net.Http.HttpRequestMessage]::new([System.Net.Http.HttpMethod]::new($method.ToUpper()), $url)
    $cred = Load-Creds
    if ($cred) {
        if ($cred.kind -eq 'api_key') { [void]$req.Headers.TryAddWithoutValidation('x-api-key', $cred.value) }
        else { [void]$req.Headers.TryAddWithoutValidation('Authorization', 'Bearer ' + $cred.value) }
    }
    return $req
}

function Start-ApiEnvelope([string]$raw) {
    $m = $null
    try { $m = $raw | ConvertFrom-Json } catch { Log "bad api envelope: $raw" 'ERR'; return }
    $url = [string]$m.url
    $method = if ($m.method) { [string]$m.method } else { 'GET' }
    try {
        # ---- multipart asset upload, streamed from this PC -----------------
        if ($m.upload) {
            $file = [string]$m.upload.file
            if (-not (Test-Path -LiteralPath $file)) { throw "file not found: $file" }
            $cred = Load-Creds
            if (-not $cred) { throw 'no roblox_api_key.txt next to the relay' }
            $req = Get-AuthRequest 'https://apis.roblox.com/assets/v1/assets' 'POST'
            $bytes = [System.IO.File]::ReadAllBytes($file)
            $mp = [System.Net.Http.MultipartFormDataContent]::new()
            $fc = [System.Net.Http.ByteArrayContent]::new($bytes)
            $fc.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::Parse('application/octet-stream')
            $mp.Add($fc, 'file', [System.IO.Path]::GetFileName($file))
            $ask = [ordered]@{
                assetType   = [string]$m.upload.assetType
                displayName = [string]$m.upload.name
                description = [string]$m.upload.description
            }
            if ($m.upload.creatorId) {
                if ([string]$m.upload.creatorKind -eq 'group') {
                    $ask.creationContext = @{ creator = @{ groupId = [string]$m.upload.creatorId } }
                } else {
                    $ask.creationContext = @{ creator = @{ userId = [string]$m.upload.creatorId } }
                }
            }
            $sc = [System.Net.Http.StringContent]::new(($ask | ConvertTo-Json -Depth 6 -Compress),
                                                      [System.Text.Encoding]::UTF8, 'application/json')
            $mp.Add($sc, 'request')
            $req.Content = $mp
            $task = $script:Http.SendAsync($req)
            [void]$script:ApiPending.Add([pscustomobject]@{ Id = $m.id; Task = $task; Url = 'upload:' + $file
                                                          Method = 'UPLOAD'; Mode = 'upload'; Out = $null })
            Log ('UPLOAD {0} ({1:N0} bytes) started' -f $file, $bytes.Length) 'INFO'
            return
        }

        # ---- place file download, written on this PC -----------------------
        if ($method -eq 'DOWNLOAD') {
            $req = Get-AuthRequest $url 'GET'
            $task = $script:Http.SendAsync($req)
            [void]$script:ApiPending.Add([pscustomobject]@{ Id = $m.id; Task = $task; Url = $url
                                                          Method = 'DOWNLOAD'; Mode = 'download'; Out = [string]$m.download.out })
            Log ('DOWNLOAD {0} started' -f $url) 'INFO'
            return
        }

        # ---- ordinary REST call -------------------------------------------
        $req = [System.Net.Http.HttpRequestMessage]::new([System.Net.Http.HttpMethod]::new($method.ToUpper()), $url)
        $hasAuth = $false
        $ct = 'application/json'
        if ($m.headers) {
            foreach ($h in $m.headers.PSObject.Properties) {
                $hn = [string]$h.Name
                $hv = [string]$h.Value
                if ($hn -ieq 'Authorization' -or $hn -ieq 'x-api-key') { $hasAuth = $true }
                if ($hn -ieq 'Content-Type') { $ct = $hv; continue }
                [void]$req.Headers.TryAddWithoutValidation($hn, $hv)
            }
        }
        if (-not $hasAuth -and $url -match 'roblox\.com') {
            $cred = Load-Creds
            if ($cred) {
                if ($cred.kind -eq 'api_key') { [void]$req.Headers.TryAddWithoutValidation('x-api-key', $cred.value) }
                else { [void]$req.Headers.TryAddWithoutValidation('Authorization', 'Bearer ' + $cred.value) }
            }
        }
        $bodyText = if ($m.body) { [string]$m.body } else { '' }
        if ($bodyText) { $req.Content = [System.Net.Http.StringContent]::new($bodyText, [System.Text.Encoding]::UTF8, $ct) }

        $task = $script:Http.SendAsync($req)
        [void]$script:ApiPending.Add([pscustomobject]@{ Id = $m.id; Task = $task; Url = $url
                                                      Method = $method; Mode = 'normal'; Out = $null })
        Log ('API {0} {1} started ({2} in flight)' -f $method, $url, $script:ApiPending.Count) 'TRACE'
    } catch {
        $ex = $_.Exception
        while ($ex.InnerException) { $ex = $ex.InnerException }
        $script:Stat.errors++
        Send-ApiResponse $m.id 0 (@{ error = $ex.Message } | ConvertTo-Json -Compress)
        Log ('API {0} could not start: {1}' -f $url, $ex.Message) 'ERR'
    }
}

function Send-ApiResponse($id, [int]$status, [string]$body) {
    $payload = @{ type = 'api_response'; id = $id; status = $status; body = $body } |
               ConvertTo-Json -Depth 4 -Compress
    try { [void](Post-Bridge '/relay/send' $payload) }
    catch { Log "returning api_response failed: $($_.Exception.Message)" 'ERR' }
}

function Complete-ApiEnvelopes {
    if ($script:ApiPending.Count -eq 0) { return }
    for ($i = $script:ApiPending.Count - 1; $i -ge 0; $i--) {
        $entry = $script:ApiPending[$i]
        if (-not $entry.Task.IsCompleted) { continue }
        $script:ApiPending.RemoveAt($i)
        $status = 0
        $body = ''
        try {
            $resp = $entry.Task.Result
            $status = [int]$resp.StatusCode
            if ($entry.Mode -eq 'download') {
                $bytes = $resp.Content.ReadAsByteArrayAsync().Result
                $out = $entry.Out
                if (-not $out) { $out = Join-Path $env:TEMP 'roblox_download.rbxl' }
                $dir = Split-Path -Parent $out
                if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
                [System.IO.File]::WriteAllBytes($out, $bytes)
                $body = (@{ saved = $out; bytes = $bytes.Length; status = $status } | ConvertTo-Json -Compress)
                Log ('DOWNLOAD -> {0} ({1:N0} bytes, HTTP {2})' -f $out, $bytes.Length, $status) 'INFO'
            } else {
                $body = $resp.Content.ReadAsStringAsync().Result
                Log ('API {0} {1} -> {2} ({3} bytes)' -f $entry.Method, $entry.Url, $status, $body.Length) 'TRACE'
            }
            $script:Stat.apiCalls++
        } catch {
            $ex = $_.Exception
            while ($ex.InnerException) { $ex = $ex.InnerException }
            $status = 0
            $body = (@{ error = $ex.Message } | ConvertTo-Json -Compress)
            $script:Stat.errors++
            Log ('API {0} failed: {1}' -f $entry.Url, $ex.Message) 'ERR'
        }
        Send-ApiResponse $entry.Id $status $body
    }
}

# ===========================================================================
#  Bridge session
# ===========================================================================
function Join-Bridge {
    $hello = Post-Bridge '/relay/hello' '{}'
    if ($hello.Status -ne 200) {
        $snip = $hello.Body
        if ($snip -and $snip.Length -gt 300) { $snip = $snip.Substring(0, 300) }
        Log ('bridge hello returned HTTP {0}: {1}' -f $hello.Status, $snip) 'ERR'
        return $false
    }
    $o = $hello.Body | ConvertFrom-Json
    $script:Rid = $o.relay_id
    Log ('joined bridge as {0}' -f $script:Rid) 'OK'
    return $true
}

function Start-Poll {
    $uri = Get-BridgeUri ('/relay/poll?hold=' + $PollHold)
    $script:Http.PostAsync($uri, (New-JsonContent '{}'))
}

function Handle-PollResponse($task) {
    if ($task.IsFaulted) {
        $ex = $task.Exception.GetBaseException()
        Log "poll failed: $($ex.Message)" 'ERR'
        $script:Stat.errors++
        Start-Sleep -Seconds 1
        if (-not (Join-Bridge)) { Start-Sleep -Seconds 2 }
        return
    }
    $resp = $task.Result
    $code = [int]$resp.StatusCode
    $text = $resp.Content.ReadAsStringAsync().Result
    if ($code -eq 409) {
        Log 'bridge forgot this relay session - rejoining' 'WARN'
        if (-not (Join-Bridge)) { Start-Sleep -Seconds 2 }
        return
    }
    if ($code -ne 200) {
        $snip = $text
        if ($snip -and $snip.Length -gt 200) { $snip = $snip.Substring(0, 200) }
        Log ('poll HTTP {0}: {1}' -f $code, $snip) 'ERR'
        Start-Sleep -Milliseconds 800
        return
    }
    if (-not $text) { return }
    $o = $null
    try { $o = $text | ConvertFrom-Json } catch { Log 'poll returned non-JSON' 'ERR'; return }
    if (-not $o.messages) { return }
    foreach ($m in $o.messages) {
        if ($m.kind -eq 'api') {
            Start-ApiEnvelope ([string]$m.raw)
        }
        elseif ($m.kind -eq 'rpc') {
            $line = [string]$m.raw
            if (-not (Test-StudioAlive)) {
                Log 'Studio link dropped - reconnecting before forwarding this call' 'WARN'
                Close-Studio
                if (-not (Connect-Studio)) { Log 'reconnect failed, request dropped' 'ERR'; continue }
                if (-not (Join-Bridge)) { Start-Sleep -Milliseconds 500 }
            }
            Log ('-> studio {0}' -f $line.Substring(0, [Math]::Min(150, $line.Length))) 'TRACE'
            [void](Send-Studio $line)
        }
        else { Log ("unknown message kind: {0}" -f $m.kind) 'WARN' }
    }
}

# ===========================================================================
#  Preflight: check every hop and print a verdict
# ===========================================================================
function Invoke-Preflight {
    $results = New-Object System.Collections.ArrayList
    function Add-Check($name, $ok, $detail) {
        [void]$results.Add([pscustomobject]@{ Check = $name; Result = $(if ($ok) { 'PASS' } else { 'FAIL' }); Detail = $detail })
    }

    # 1. PowerShell version
    Add-Check 'PowerShell' ($PSVersionTable.PSVersion.Major -ge 5) ("v" + $PSVersionTable.PSVersion.ToString())

    # 2. Studio process
    $studio = Get-Process -Name RobloxStudioBeta -ErrorAction SilentlyContinue
    Add-Check 'Roblox Studio running' ($null -ne $studio) ($(if ($studio) { "pid " + ($studio.Id -join ',') } else { 'launch Studio and open a place' }))

    # 3. mcp.bat present
    $bat = Join-Path (Join-Path $env:LOCALAPPDATA 'Roblox') 'mcp.bat'
    Add-Check 'mcp.bat present' (Test-Path -LiteralPath $bat) $bat

    # 4. StudioMCP.exe present
    $bin = Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'Roblox\Versions') -Recurse -Filter 'StudioMCP.exe' -ErrorAction SilentlyContinue |
           Select-Object -First 1
    Add-Check 'StudioMCP.exe present' ($null -ne $bin) ($(if ($bin) { $bin.FullName } else { 'update Roblox Studio' }))

    # 5. local MCP port
    $port = Test-LocalPort 13469
    Add-Check 'port 13469 listening' $port ($(if ($port) { 'Studio MCP socket is up' } else { 'normal before the first tool call; the relay can start mcp.bat' }))

    # 6. outbound internet + bridge reachability
    try {
        $ping = $script:Http.GetAsync("$($script:Base)/ping").Result
        $ok = [int]$ping.StatusCode -eq 200
        Add-Check 'bridge reachable' $ok ("HTTP " + [int]$ping.StatusCode + " -> " + $script:Base)
    } catch {
        $ex = $_.Exception; while ($ex.InnerException) { $ex = $ex.InnerException }
        Add-Check 'bridge reachable' $false $ex.Message
    }

    # 7. credential file
    $key = Join-Path $PSScriptRoot 'roblox_api_key.txt'
    $hasKey = (Test-Path -LiteralPath $key) -and ((Get-Content -LiteralPath $key -Raw).Trim().Length -gt 10)
    Add-Check 'Roblox API key file' $hasKey $key

    # 8. Cloud API auth (only if a key exists)
    if ($hasKey) {
        try {
            $cred = Load-Creds
            $req = [System.Net.Http.HttpRequestMessage]::new([System.Net.Http.HttpMethod]::Get, 'https://apis.roblox.com/cloud/v2/universes/0/places')
            if ($cred.kind -eq 'api_key') { [void]$req.Headers.TryAddWithoutValidation('x-api-key', $cred.value) }
            else { [void]$req.Headers.TryAddWithoutValidation('Authorization', 'Bearer ' + $cred.value) }
            $resp = $script:Http.SendAsync($req).Result
            $code = [int]$resp.StatusCode
            # 401/403 = reached Roblox but the key is wrong; 404/400 = key accepted
            Add-Check 'Cloud API auth' ($code -ne 401 -and $code -ne 403) ("HTTP $code (401/403 means the key is invalid)")
        } catch {
            $ex = $_.Exception; while ($ex.InnerException) { $ex = $ex.InnerException }
            Add-Check 'Cloud API auth' $false $ex.Message
        }
    }

    Write-Host ''
    Write-Host '  PREFLIGHT REPORT' -ForegroundColor Cyan
    Write-Host '  ----------------' -ForegroundColor Cyan
    foreach ($r in $results) {
        $color = if ($r.Result -eq 'PASS') { 'Green' } else { 'Red' }
        Write-Host ("  [{0}] {1,-24} {2}" -f $r.Result, $r.Check, $r.Detail) -ForegroundColor $color
    }
    $fails = ($results | Where-Object { $_.Result -eq 'FAIL' }).Count
    Write-Host ''
    if ($fails -eq 0) { Write-Host '  All checks passed - start the relay without -Preflight.' -ForegroundColor Green }
    else { Write-Host ("  {0} check(s) failed - fix the red lines above." -f $fails) -ForegroundColor Yellow }
    exit 0
}

# ===========================================================================
#  main
# ===========================================================================
Log '====================================================='
Log ' Arena  ->  Roblox Studio relay v4'
Log " bridge    : $($script:Base)"
Log " transport : $Transport (websocket 13469 / stdio mcp.bat)"
Log " log       : $($script:LogFile)"
Log '====================================================='

if ($Preflight) { Invoke-Preflight }

try {
    $ping = $script:Http.GetAsync("$($script:Base)/ping").Result
    Log ('bridge reachable: HTTP {0}' -f [int]$ping.StatusCode) 'OK'
} catch {
    $ex = $_.Exception
    while ($ex.InnerException) { $ex = $ex.InnerException }
    Log "CANNOT reach the bridge at $($script:Base) : $($ex.Message)" 'ERR'
    Log 'Check the URL / internet connection, or whether the Arena session is still running.' 'ERR'
    exit 1
}

if (-not (Connect-Studio)) { exit 1 }

# join triggers the bridge-side handshake: initialize -> tools/list -> studios
if (-not (Join-Bridge)) { exit 1 }

Log 'READY - waiting for orders from the Arena agent (Ctrl+C to stop)' 'OK'
$lastStatus = Get-Date
$pollTask = Start-Poll
$reconnects = 0
$running = $true

while ($running) {
    try {
        [System.Threading.Tasks.Task[]]$tasks = @($pollTask, $script:RecvTask)
        foreach ($ap in $script:ApiPending) { $tasks += $ap.Task }
        $idx = [System.Threading.Tasks.Task]::WaitAny($tasks, 1500)
        if ($script:ApiPending.Count -gt 0) { Complete-ApiEnvelopes }

        if ($idx -eq 0) {
            Handle-PollResponse $pollTask
            if ($script:Rid) { $pollTask = Start-Poll }
        }
        elseif ($idx -eq 1) {
            $msg = $null
            if ($script:RecvTask.IsFaulted) {
                $ex = $script:RecvTask.Exception.GetBaseException()
                Log "studio link error: $($ex.Message)" 'ERR'
            } else {
                $msg = Read-StudioMessage
            }
            if ($null -eq $msg) {
                Log 'studio link closed - reconnecting' 'WARN'
                if ($script:Stderr -and $script:Stderr.Length -gt 0) {
                    $tail = $script:Stderr.ToString().Trim()
                    if ($tail.Length -gt 400) { $tail = $tail.Substring($tail.Length - 400) }
                    Log ("studio stderr: " + $tail) 'WARN'
                }
                Close-Studio
                if (Connect-Studio) {
                    $reconnects++
                    if (-not (Join-Bridge)) { Start-Sleep -Seconds 1 }
                } else {
                    Start-Sleep -Seconds 3
                }
            } else {
                $script:Stat.fromStudio++
                Log ('<- studio {0}' -f $msg.Substring(0, [Math]::Min(150, $msg.Length))) 'TRACE'
                # forward verbatim - Studio's JSON is the request body untouched
                $r = Post-Bridge '/relay/send' $msg
                if ($r.Status -ne 200) { Log ('relay/send HTTP {0}' -f $r.Status) 'WARN' }
                Start-Receive
            }
        }

        if (((Get-Date) - $lastStatus).TotalSeconds -ge 60) {
            $lastStatus = Get-Date
            Log ('alive [{0}] - to-studio {1}, from-studio {2}, api {3}, errors {4}, reconnects {5}' -f `
                 $script:Mode, $script:Stat.toStudio, $script:Stat.fromStudio, $script:Stat.apiCalls,
                 $script:Stat.errors, $reconnects) 'TRACE'
        }
    } catch {
        $ex = $_.Exception
        while ($ex.InnerException) { $ex = $ex.InnerException }
        Log "loop error: $($ex.Message)" 'ERR'
        $script:Stat.errors++
        Start-Sleep -Milliseconds 500
        try { if ($script:RecvTask -and $script:RecvTask.IsCompleted -and (Test-StudioAlive)) { Start-Receive } } catch { }
    }
}
