<#
  BOOTSTRAP.ps1 - stable entry point for the Arena -> Roblox Studio relay.

  Published at a URL that never changes (GitHub raw), so the one-line command
  keeps working across Arena sessions:

      iwr -UseBasicParsing "https://raw.githubusercontent.com/<owner>/<repo>/main/bridge/relay/BOOTSTRAP.ps1" -OutFile "$env:TEMP\arena.ps1"; & "$env:TEMP\arena.ps1"

  It probes the known bridges, picks the live one, then hands over to that
  bridge's own setup.ps1 (which downloads the relay and starts it).

  If the Arena session is dead, it says so instead of throwing a 502 stack.

  Parameters
      -BridgeUrl  force a bridge URL (skips discovery)
      -Token      relay token for that bridge
      -Extra      additional bridge URLs to probe
      -List       print the URLs it would try, then exit
#>
[CmdletBinding()]
param(
    [string]$BridgeUrl = "",
    [string]$Token     = "",
    [string[]]$Extra   = @(),
    [switch]$List
)

$ErrorActionPreference = 'Continue'
try { [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12 } catch { }
try { [Net.ServicePointManager]::Expect100Continue = $false } catch { }

function Say($m, $c = 'Gray') { Write-Host $m -ForegroundColor $c }

# ------------------------------------------------------------------ bridges
# >>> BRIDGE_DEFAULTS
$defaults = @(
    @{ url = 'https://8788-iy8w5dbt2nw4x9qqg772j.e2b.app'; token = 'bridge-secret-iy8w5dbt2nw4x9qqg772j' }
)
# <<< BRIDGE_DEFAULTS

$candidates = New-Object System.Collections.ArrayList
function Add-Candidate([string]$url, [string]$tok) {
    if (-not $url) { return }
    if ($url -match '__BRIDGE') { return }          # un-baked template
    $url = $url.TrimEnd('/')
    if ($url -notmatch '^https?://') { $url = 'https://' + $url }
    foreach ($c in $candidates) { if ($c.Url -eq $url) { return } }
    [void]$candidates.Add([pscustomobject]@{ Url = $url; Token = $tok })
}

if ($BridgeUrl) { Add-Candidate $BridgeUrl $Token }
foreach ($e in $Extra) { Add-Candidate $e $Token }
foreach ($d in $defaults) {
    $tok = if ($Token) { $Token } else { $d.token }
    Add-Candidate $d.url $tok
}
if (-not $Token) {
    foreach ($d in $defaults) { if ($d.token -and $d.token -notmatch '__BRIDGE') { $Token = $d.token; break } }
}

if ($List) {
    Say 'candidate bridges:' 'Cyan'
    foreach ($c in $candidates) { Say ('  ' + $c.Url) }
    exit 0
}

Say ''
Say '==============================================' 'Cyan'
Say '  Arena  ->  Roblox Studio   bootstrap' 'Cyan'
Say '==============================================' 'Cyan'
Say ''

# ------------------------------------------------------------------- probe
$live = $null
foreach ($c in $candidates) {
    try {
        $ping = Invoke-RestMethod -Uri ($c.Url + '/ping') -UseBasicParsing -TimeoutSec 12
        if ($ping -and $ping.pong) {
            Say ('bridge LIVE : {0} (v{1})' -f $c.Url, $ping.v) 'Green'
            $live = $c
            break
        }
    } catch {
        $ex = $_.Exception
        while ($ex.InnerException) { $ex = $ex.InnerException }
        Say ('dead        : {0}  ({1})' -f $c.Url, $ex.Message) 'DarkGray'
    }
}

if (-not $live) {
    Say ''
    Say 'No live Arena bridge answered.' 'Red'
    Say 'That means the Arena session hosting the bridge is not running right now.' 'Yellow'
    Say 'Ask the agent for a fresh bridge URL, then run:' 'Yellow'
    Say '  & "$env:TEMP\arena.ps1" -BridgeUrl "https://....e2b.app" -Token "..."' 'Gray'
    Say ''
    Say 'Nothing on your computer is broken - the relay itself is already on disk at' 'Gray'
    Say '  %USERPROFILE%\ArenaRoblox\roblox_relay.ps1' 'Gray'
    Say 'and works again as soon as a bridge is up.' 'Gray'
    exit 1
}

# ---------------------------------------------------------------- hand over
$setupUrl = $live.Url + '/relay/file/setup.ps1?token=' + [uri]::EscapeDataString($live.Token)
$setupLocal = Join-Path $env:TEMP 'arena_setup.ps1'
try {
    Say ('downloading : setup.ps1 from {0}' -f $live.Url) 'Gray'
    $resp = Invoke-WebRequest -Uri $setupUrl -UseBasicParsing -TimeoutSec 60
    $bytes = $resp.Content
    if ($bytes -is [string]) { $bytes = [System.Text.Encoding]::UTF8.GetBytes($bytes) }
    if ($bytes.Length -lt 500) { throw ('setup.ps1 looks wrong (' + $bytes.Length + ' bytes)') }
    [System.IO.File]::WriteAllBytes($setupLocal, $bytes)
} catch {
    $ex = $_.Exception
    while ($ex.InnerException) { $ex = $ex.InnerException }
    Say ('could not download setup.ps1: {0}' -f $ex.Message) 'Red'
    exit 1
}

Say 'handing over to the bridge installer...' 'Cyan'
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $setupLocal -BridgeUrl $live.Url -Token $live.Token
