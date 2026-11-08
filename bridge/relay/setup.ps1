<#
  setup.ps1 - one-shot installer for the Arena -> Roblox Studio relay.

  Run it with a single line in PowerShell (it downloads itself nothing;
  everything is embedded or fetched from your bridge):

      powershell -ExecutionPolicy Bypass -Command "iwr -UseB https://<bridge>/relay/file/setup.ps1 -OutFile $env:TEMP\arena_setup.ps1; & $env:TEMP\arena_setup.ps1"

  What it does
    1. creates %USERPROFILE%\ArenaRoblox
    2. downloads roblox_relay.ps1 and the Roblox API key from the bridge
    3. writes a config file, then starts the relay in a visible window
#>
[CmdletBinding()]
param(
    [string]$BridgeUrl = "https://8788-iy8w5dbt2nw4x9qqg772j.e2b.app",
    [string]$Token     = "bridge-secret-iy8w5dbt2nw4x9qqg772j",
    [string]$Dir       = (Join-Path $env:USERPROFILE 'ArenaRoblox'),
    [switch]$InstallPlugin,
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'
try { [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12 } catch { }

function Say($msg, $color = 'Gray') { Write-Host $msg -ForegroundColor $color }

$bridge = $BridgeUrl.TrimEnd('/')
Say '==============================================' 'Cyan'
Say '  Arena  ->  Roblox Studio  : installer' 'Cyan'
Say '==============================================' 'Cyan'

# ---- 1. location ----------------------------------------------------------
if (-not (Test-Path -LiteralPath $Dir)) {
    New-Item -ItemType Directory -Path $Dir -Force | Out-Null
}
Say "install dir : $Dir"

# ---- 2. fetch the relay + credential --------------------------------------
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

# ---- 3. optional Studio plugin install ------------------------------------
if ($InstallPlugin) {
    $plugins = Join-Path $env:LOCALAPPDATA 'Roblox\Plugins'
    if (-not (Test-Path -LiteralPath $plugins)) { New-Item -ItemType Directory -Path $plugins -Force | Out-Null }
    Say "plugins dir : $plugins (drop the MCP plugin here if Studio asks for it)" 'Yellow'
}

# ---- 4. connectivity pre-check -------------------------------------------
try {
    $ping = Invoke-RestMethod -Uri "$bridge/ping" -UseBasicParsing -TimeoutSec 15
    Say ("bridge      : OK (v{0})" -f $ping.v) 'Green'
} catch {
    Say ("bridge      : UNREACHABLE - {0}" -f $_.Exception.Message) 'Red'
}

if (-not (Get-Process -Name RobloxStudioBeta -ErrorAction SilentlyContinue)) {
    Say 'Roblox Studio is not running. Open a place, then start the relay.' 'Yellow'
} else {
    Say 'Roblox Studio is running.' 'Green'
}

$relay = Join-Path $Dir 'roblox_relay.ps1'
Say ''
Say 'Starting the relay. Leave this window open; Ctrl+C stops it.' 'Cyan'
if (-not $NoStart) {
    $cmd = 'powershell -NoExit -ExecutionPolicy Bypass -File "' + $relay + '" -BridgeUrl "' + $bridge + '" -Token "' + $Token + '"'
    Start-Process -FilePath 'powershell.exe' -ArgumentList $cmd -WorkingDirectory $Dir
    Say 'relay window opened. Watch it for "READY".' 'Green'
    Say ("status page : {0}/" -f $bridge) 'Cyan'
}
