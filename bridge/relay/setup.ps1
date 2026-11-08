<#
  setup.ps1 - one-shot installer + preflight for the Arena -> Roblox Studio relay.

  One line in PowerShell (nothing to download by hand):

      iwr -UseBasicParsing "https://8788-iy8w5dbt2nw4x9qqg772j.e2b.app/relay/file/setup.ps1" -OutFile "$env:TEMP\arena.ps1"; & "$env:TEMP\arena.ps1"

  What it does
    1. creates %USERPROFILE%\ArenaRoblox
    2. downloads roblox_relay.ps1 + your Roblox API key from the bridge
    3. runs the relay's -Preflight check and prints a PASS/FAIL table
    4. opens the relay window (skip with -NoStart)
#>
[CmdletBinding()]
param(
    [string]$BridgeUrl = "https://8788-iy8w5dbt2nw4x9qqg772j.e2b.app",
    [string]$Token     = "bridge-secret-iy8w5dbt2nw4x9qqg772j",
    [string]$Dir       = (Join-Path $env:USERPROFILE 'ArenaRoblox'),
    [switch]$NoStart,
    [switch]$SkipPreflight
)

$ErrorActionPreference = 'Stop'
try { [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12 } catch { }

function Say($msg, $color = 'Gray') { Write-Host $msg -ForegroundColor $color }

$bridge = $BridgeUrl.TrimEnd('/')
Say ''
Say '==============================================' 'Cyan'
Say '  Arena  ->  Roblox Studio   installer' 'Cyan'
Say '==============================================' 'Cyan'
Say ''

# ---- 1. install dir -------------------------------------------------------
if (-not (Test-Path -LiteralPath $Dir)) { New-Item -ItemType Directory -Path $Dir -Force | Out-Null }
Say "install dir : $Dir"

# ---- 2. is the bridge even up? -------------------------------------------
try {
    $ping = Invoke-RestMethod -Uri "$bridge/ping" -UseBasicParsing -TimeoutSec 20
    Say ("bridge      : OK (v{0})" -f $ping.v) 'Green'
} catch {
    Say ("bridge      : UNREACHABLE - {0}" -f $_.Exception.Message) 'Red'
    Say 'The Arena session must be running. Ask the agent to check the bridge, then retry.' 'Yellow'
    exit 1
}

# ---- 3. download the relay + credential ----------------------------------
$files = @('roblox_relay.ps1', 'roblox_api_key.txt')
foreach ($f in $files) {
    $url = "$bridge/relay/file/$f`?token=$([uri]::EscapeDataString($Token))"
    $dest = Join-Path $Dir $f
    try {
        $resp = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 60
        $bytes = $resp.Content
        if ($bytes -is [string]) { $bytes = [System.Text.Encoding]::UTF8.GetBytes($bytes) }
        [System.IO.File]::WriteAllBytes($dest, $bytes)
        Say ("downloaded  : {0} ({1} bytes)" -f $f, $bytes.Length) 'Green'
    } catch {
        Say ("FAILED      : {0} -> {1}" -f $f, $_.Exception.Message) 'Red'
        if ($f -eq 'roblox_relay.ps1') { throw 'cannot continue without the relay script' }
    }
}

$relay = Join-Path $Dir 'roblox_relay.ps1'
if (-not (Test-Path -LiteralPath $relay)) { throw "relay script missing at $relay" }

# ---- 4. preflight ---------------------------------------------------------
if (-not $SkipPreflight) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $relay -BridgeUrl $bridge -Token $Token -Preflight
}

# ---- 5. start the relay ---------------------------------------------------
Say ''
if ($NoStart) {
    Say "Preflight only. To run the relay later:" 'Cyan'
    Say ('  powershell -ExecutionPolicy Bypass -File "{0}" -BridgeUrl "{1}" -Token "{2}"' -f $relay, $bridge, $Token) 'Gray'
    exit 0
}

Say 'Starting the relay in its own window. Leave it open.' 'Cyan'
$cmd = 'powershell -NoExit -ExecutionPolicy Bypass -File "' + $relay + '" -BridgeUrl "' + $bridge + '" -Token "' + $Token + '"'
Start-Process -FilePath 'powershell.exe' -ArgumentList $cmd -WorkingDirectory $Dir
Say ''
Say 'The relay window will print:  READY - waiting for orders from the Arena agent' 'Green'
Say 'When you see READY, go back to the chat and say: connected' 'Green'
Say ("Status page : {0}/" -f $bridge) 'Cyan'
Say ("Relay log   : {0}\relay.log" -f $Dir) 'Cyan'
Say ''
