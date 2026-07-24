<#
  hollerback — Windows installer / updater.

  Pulls the plugin straight from the broker (no file copy, no SSH), installs it
  at USER scope, points it at the broker, and pins the monitor to an absolute
  python.exe path so it does not depend on the flaky WindowsApps `python3` alias.

  Re-run this any time to update. Usage:
      powershell -ExecutionPolicy Bypass -File install-windows.ps1
      powershell -ExecutionPolicy Bypass -File install-windows.ps1 -AgentName frontend
#>
[CmdletBinding()]
param(
  [string]$Broker    = "http://127.0.0.1:8850",
  [string]$AgentName = "frontend"
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

Write-Host "==> broker: $Broker   agent: $AgentName" -ForegroundColor Cyan

# --- 1. reachability -------------------------------------------------------
try {
  $health = Invoke-RestMethod -Uri "$Broker/v1/health" -TimeoutSec 10
  Write-Host "    broker reachable, ok=$($health.ok)" -ForegroundColor Green
} catch {
  Write-Host "    CANNOT REACH BROKER at $Broker" -ForegroundColor Red
  Write-Host "    Is Tailscale up, and is hollerback-broker running on ada?" -ForegroundColor Red
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
$tmpZip = Join-Path $env:TEMP "hollerback-plugin.zip"
Invoke-WebRequest -Uri "$Broker/v1/plugin.zip" -OutFile $tmpZip -TimeoutSec 30
if (Test-Path $Dest) { Remove-Item -Recurse -Force $Dest }
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
Expand-Archive -Path $tmpZip -DestinationPath $Dest -Force
Remove-Item $tmpZip -Force
Write-Host "    installed to $Dest" -ForegroundColor Green

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

# --- 5. write plugin config into settings.json ------------------------------
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

$options = [ordered]@{ AGENT_NAME = $AgentName; BROKER_URL = $Broker }
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
assert o['AGENT_NAME'] and o['BROKER_URL'], 'AGENT_NAME/BROKER_URL missing'
print('ok', o['AGENT_NAME'])
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
Write-Host "==> smoke test: connecting as '$AgentName' for 4s ..." -ForegroundColor Cyan
$p = Start-Process -FilePath $py `
      -ArgumentList @((Join-Path $Dest "bin\listen.py")) `
      -NoNewWindow -PassThru `
      -RedirectStandardError (Join-Path $env:TEMP "hollerback-smoke.err") `
      -RedirectStandardOutput (Join-Path $env:TEMP "hollerback-smoke.out")
Start-Sleep -Seconds 4
if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force }
$err = Get-Content (Join-Path $env:TEMP "hollerback-smoke.err") -Raw -ErrorAction SilentlyContinue
if ($err -match "connected to") {
  Write-Host "    listener connected OK" -ForegroundColor Green
} else {
  Write-Host "    listener did NOT connect. stderr was:" -ForegroundColor Yellow
  Write-Host "    $err" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Done. Now, in your frontend Claude Code session:" -ForegroundColor Cyan
Write-Host "  * restart it (or run /reload-plugins)" -ForegroundColor White
Write-Host "  * the status line should show '1 monitor'" -ForegroundColor White
Write-Host "  * that project dir must be trusted, or monitors are skipped" -ForegroundColor White
