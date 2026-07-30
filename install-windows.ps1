<#
  hollerback — Windows installer / updater.

  Pulls the plugin straight from the broker (no file copy, no SSH), installs it
  at USER scope, points it at the broker, and pins the monitor to an absolute
  python.exe path so it does not depend on the flaky WindowsApps `python3` alias.

  Re-run this any time to update. Usage:

      & ([scriptblock]::Create((iwr https://raw.githubusercontent.com/dbrentley/hollerback/main/install-windows.ps1 -UseBasicParsing).Content)) -Broker http://100.64.0.5:8850

  There is nothing to name. A session identifies itself as
  <host>:<project-dir>#<tag>, unique even when two sessions share a directory, and says what
  it does at runtime via announce(), which peers read back with discover(). One
  install per machine; every workspace on it is its own agent.
#>
[CmdletBinding()]
param(
  [string]$Broker    = "http://127.0.0.1:8850",
  [string]$AgentName = ""      # escape hatch; normally auto-derived
)

$ErrorActionPreference = "Stop"
$PluginSource = "hollerback@skills-dir"

# PowerShell 5.1's `Set-Content -Encoding UTF8` writes a BOM, and a BOM at the
# start of a JSON file breaks strict parsers (python's json.loads reports
# "Expecting value: line 1 column 1"). Always write JSON without one.
function Write-JsonNoBom {
  param([string]$Path, [string]$Json)
  $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
  [System.IO.File]::WriteAllText($Path, $Json, $utf8NoBom)
}
$SkillsDir    = Join-Path $env:USERPROFILE ".claude\skills"
$Dest         = Join-Path $SkillsDir "hollerback"
$SettingsPath = Join-Path $env:USERPROFILE ".claude\settings.json"

Write-Host "==> broker: $Broker" -ForegroundColor Cyan

# --- 1. reachability -------------------------------------------------------
try {
  $health = Invoke-RestMethod -Uri "$Broker/v1/health" -TimeoutSec 10
  Write-Host "    broker reachable, ok=$($health.ok)" -ForegroundColor Green
} catch {
  Write-Host "    CANNOT REACH BROKER at $Broker" -ForegroundColor Red
  Write-Host "    Is the network up, and is hollerback-broker running on that host?" -ForegroundColor Red
  exit 1
}

# --- 2. locate a real python.exe -------------------------------------------
$py = $null
foreach ($c in @("python", "python3")) {
  $cmd = Get-Command $c -ErrorAction SilentlyContinue
  if ($cmd) {
    try {
      $v = & $cmd.Source --version 2>&1
      if ($v -match "Python 3") { $py = $cmd.Source; break }
    } catch { }
  }
}
if (-not $py) { Write-Host "    No working Python 3 found on PATH." -ForegroundColor Red; exit 1 }
Write-Host "    python: $py" -ForegroundColor Green

# --- 3. download + extract --------------------------------------------------
# GitHub first, broker second -- same order as install.sh. A broker is a
# long-lived deployment that lags the repo, so preferring it pairs a current
# installer with a stale plugin.
$Repo = if ($env:HOLLERBACK_REPO) { $env:HOLLERBACK_REPO } else { "dbrentley/hollerback" }
$Ref  = if ($env:HOLLERBACK_REF)  { $env:HOLLERBACK_REF }  else { "main" }
$tmpZip = Join-Path $env:TEMP "hollerback-plugin.zip"
$srcDesc = ""
try {
  Invoke-WebRequest -Uri "https://codeload.github.com/$Repo/zip/refs/heads/$Ref" `
      -OutFile $tmpZip -TimeoutSec 90 -UseBasicParsing
  $srcDesc = "github ($Repo@$Ref)"
} catch {
  try {
    Invoke-WebRequest -Uri "$Broker/v1/plugin.zip" -OutFile $tmpZip -TimeoutSec 30 -UseBasicParsing
    $srcDesc = "broker"
  } catch {
    Write-Host "    could not fetch the plugin from GitHub or from $Broker" -ForegroundColor Red
    exit 1
  }
}
if (Test-Path $Dest) { Remove-Item -Recurse -Force $Dest }
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
if ($srcDesc -eq "broker") {
  Expand-Archive -Path $tmpZip -DestinationPath $Dest -Force
} else {
  # The GitHub zip is the whole repo; the plugin is one directory inside it.
  $tmpX = Join-Path $env:TEMP ("hollerback-src-" + [guid]::NewGuid().ToString("N"))
  Expand-Archive -Path $tmpZip -DestinationPath $tmpX -Force
  $pluginDir = Get-ChildItem -Path $tmpX -Directory | ForEach-Object { Join-Path $_.FullName "plugin" } |
               Where-Object { Test-Path $_ } | Select-Object -First 1
  if (-not $pluginDir) { Write-Host "    zip has no plugin/ directory" -ForegroundColor Red; exit 1 }
  Copy-Item -Path (Join-Path $pluginDir "*") -Destination $Dest -Recurse -Force
  Remove-Item -Recurse -Force $tmpX
}
Remove-Item $tmpZip -Force
Write-Host "    installed to $Dest (from $srcDesc)" -ForegroundColor Green

# --- 4. pin the monitor to the absolute interpreter -------------------------
# Claude Code refuses to arm a monitor whose command contains ${user_config.*},
# so listen.py reads its own config; the command only needs the interpreter.
$monitorsPath = Join-Path $Dest "monitors\monitors.json"

# monitors.json MUST be a JSON array. Do not build it with
# `@($obj) | ConvertTo-Json` -- PowerShell 5.1 unrolls a single-element array
# through the pipeline, so it serializes as an object and Claude Code rejects
# the file with "expected array, received object". Emit the array literally.
# ConvertTo-Json on the plain string handles escaping the backslashes in $py.
$cmdJson = ConvertTo-Json -InputObject ('"' + $py + '" "${CLAUDE_PLUGIN_ROOT}/bin/listen.py"')
$monitorsJson = @"
[
  {
    "name": "inbox",
    "command": $cmdJson,
    "description": "Questions and answers from the other Claude Code session",
    "when": "always"
  }
]
"@
Write-JsonNoBom -Path $monitorsPath -Json $monitorsJson
Write-Host "    monitor command pinned to $py" -ForegroundColor Green

# --- 5. write plugin config ------------------------------------------------
if (Test-Path $SettingsPath) {
  # Don't clobber a good backup with a bad file on a repeat run.
  if (-not (Test-Path "$SettingsPath.bak.hollerback")) {
    Copy-Item $SettingsPath "$SettingsPath.bak.hollerback" -Force
  }
  try {
    $settings = Get-Content $SettingsPath -Raw | ConvertFrom-Json
  } catch {
    Write-Host "    settings.json is unparseable; recovering from backup" -ForegroundColor Yellow
    $settings = Get-Content "$SettingsPath.bak.hollerback" -Raw | ConvertFrom-Json
  }
} else {
  New-Item -ItemType Directory -Force -Path (Split-Path $SettingsPath) | Out-Null
  $settings = [PSCustomObject]@{}
}

$options = [ordered]@{ BROKER_URL = $Broker }
if ($AgentName) { $options.AGENT_NAME = $AgentName }
$entry   = [ordered]@{ options = $options }

if (-not $settings.PSObject.Properties.Name.Contains("pluginConfigs")) {
  $settings | Add-Member -NotePropertyName pluginConfigs -NotePropertyValue ([PSCustomObject]@{})
}
if ($settings.pluginConfigs.PSObject.Properties.Name -contains $PluginSource) {
  $settings.pluginConfigs.$PluginSource = [PSCustomObject]$entry
} else {
  $settings.pluginConfigs | Add-Member -NotePropertyName $PluginSource -NotePropertyValue ([PSCustomObject]$entry)
}

Write-JsonNoBom -Path $SettingsPath -Json ($settings | ConvertTo-Json -Depth 20)
Write-Host "    settings.json updated (backup: $SettingsPath.bak.hollerback)" -ForegroundColor Green

# Verify SHAPE, not just syntax. Checking only that the file parses is what let
# an object slip through where Claude Code required an array.
$verifySettings = @"
import json,sys
d=json.load(open(sys.argv[1],encoding='utf-8-sig'))
assert isinstance(d,dict), 'settings.json must be an object'
o=d['pluginConfigs']['$PluginSource']['options']
assert o['BROKER_URL'], 'BROKER_URL missing'
print('ok', o['BROKER_URL'])
"@
$verifyMonitors = @"
import json,sys
d=json.load(open(sys.argv[1],encoding='utf-8-sig'))
assert isinstance(d,list), 'monitors.json must be a JSON ARRAY, got %s' % type(d).__name__
assert d and d[0].get('name') and d[0].get('command'), 'monitor entry incomplete'
print('ok', d[0]['name'])
"@
foreach ($pair in @(@($SettingsPath, $verifySettings), @($monitorsPath, $verifyMonitors))) {
  $f = $pair[0]; $script = $pair[1]
  # Via a temp file rather than `-c` -- multi-line scripts through PowerShell
  # argument quoting are a reliable source of mystery failures.
  $tmpPy = Join-Path $env:TEMP ("hollerback-verify-" + [guid]::NewGuid().ToString("N") + ".py")
  Write-JsonNoBom -Path $tmpPy -Json $script
  $check = & $py $tmpPy $f 2>&1
  Remove-Item $tmpPy -Force -ErrorAction SilentlyContinue
  if ($LASTEXITCODE -eq 0 -and $check -match "ok") {
    Write-Host "    verified: $(Split-Path $f -Leaf) -- $check" -ForegroundColor Green
  } else {
    Write-Host "    INVALID $f" -ForegroundColor Red
    Write-Host "    $check" -ForegroundColor Red
    exit 1
  }
}

# --- 6. smoke test ----------------------------------------------------------
Write-Host "==> smoke test: connecting for 4s ..." -ForegroundColor Cyan
$env:HOLLERBACK_BROKER = $Broker
# Scratch id, then forgotten -- otherwise the smoke test registers a permanent
# agent named after whatever directory the installer was run from.
$SmokeId = "_install-smoketest"
$env:HOLLERBACK_AGENT = $SmokeId
$p = Start-Process -FilePath $py `
      -ArgumentList @((Join-Path $Dest "bin\listen.py")) `
      -NoNewWindow -PassThru `
      -RedirectStandardError (Join-Path $env:TEMP "hollerback-smoke.err") `
      -RedirectStandardOutput (Join-Path $env:TEMP "hollerback-smoke.out")
Start-Sleep -Seconds 4
if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force }
$err = Get-Content (Join-Path $env:TEMP "hollerback-smoke.err") -Raw -ErrorAction SilentlyContinue
if ($err -match "connected to") {
  Write-Host "    listener connected OK (real sessions get their id from the broker)" -ForegroundColor Green
} else {
  Write-Host "    listener did NOT connect. stderr was:" -ForegroundColor Yellow
  Write-Host "    $err" -ForegroundColor Yellow
}

Remove-Item Env:\HOLLERBACK_AGENT -ErrorAction SilentlyContinue
try {
  Invoke-RestMethod -Uri "$Broker/v1/forget" -Method Post -TimeoutSec 5 `
    -ContentType "application/json" -Body (@{agent=$SmokeId} | ConvertTo-Json) | Out-Null
} catch { }   # older brokers have no /v1/forget; harmless

Write-Host ""
Write-Host "Done. Nothing to name -- ids are <host>:<project-dir>#<tag>." -ForegroundColor Cyan
Write-Host "  * START A NEW SESSION -- /reload-plugins does NOT respawn the MCP server." -ForegroundColor White
Write-Host "  * The workspace must be TRUSTED, or monitors are silently skipped." -ForegroundColor White
Write-Host "  * Status line should show '1 monitor'; you should have 10 hollerback tools." -ForegroundColor White
Write-Host "  * In each session call announce() once, then discover() to see everyone." -ForegroundColor White
Write-Host "    Every workspace on this machine is its own agent -- no second install." -ForegroundColor White
