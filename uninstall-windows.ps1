<#
  hollerback — Windows uninstaller.

  Removes the plugin, its config entry, and its local state. Leaves the broker
  alone, and leaves files peers sent you (.hollerback\inbox\ inside your
  projects) unless you pass -PurgeInboxes.

      powershell -ExecutionPolicy Bypass -File uninstall-windows.ps1
      powershell -ExecutionPolicy Bypass -File uninstall-windows.ps1 -PurgeInboxes

  Or straight from GitHub, no download step (needs no broker and no network
  beyond fetching this file -- it only removes local files):

      & ([scriptblock]::Create((iwr https://raw.githubusercontent.com/dbrentley/hollerback/main/uninstall-windows.ps1 -UseBasicParsing).Content))
#>
[CmdletBinding()]
param([switch]$PurgeInboxes)

$ErrorActionPreference = "Stop"

# PowerShell 5.1's Set-Content -Encoding UTF8 emits a BOM, which breaks strict
# JSON parsers. Always write without one.
function Write-JsonNoBom {
  param([string]$Path, [string]$Json)
  [System.IO.File]::WriteAllText($Path, $Json, (New-Object System.Text.UTF8Encoding($false)))
}

$SkillsDir    = Join-Path $env:USERPROFILE ".claude\skills"
$SettingsPath = Join-Path $env:USERPROFILE ".claude\settings.json"
$removed = $false

Write-Host "==> uninstalling hollerback" -ForegroundColor Cyan

# Current name and the pre-rename one, so an old install goes too.
# skills/ is the installer route; plugins/ is the marketplace route.
foreach ($name in @("hollerback", "agentshare")) {
  foreach ($base in @($SkillsDir, (Join-Path $env:USERPROFILE ".claude\plugins"))) {
  $t = Join-Path $base $name
  if (Test-Path $t) {
    Remove-Item -Recurse -Force $t
    Write-Host "    removed $t" -ForegroundColor Green
    $removed = $true
  }
  }
}

if (Test-Path $SettingsPath) {
  Copy-Item $SettingsPath "$SettingsPath.bak.hollerback-uninstall" -Force
  try {
    $s = Get-Content $SettingsPath -Raw | ConvertFrom-Json
    $gone = @()
    # Match the PLUGIN half of the source id, not the whole string. Config is
    # keyed "<plugin>@<marketplace>", so the installer writes hollerback@skills-dir
    # while `claude plugin install` writes hollerback@hollerback, and a fork writes
    # hollerback@<their-marketplace>. Listing exact ids left marketplace installs
    # behind on every uninstall.
    if ($s.PSObject.Properties.Name -contains "pluginConfigs") {
      foreach ($k in @($s.pluginConfigs.PSObject.Properties.Name)) {
        if ($k -match '^(hollerback|agentshare)@') {
          $s.pluginConfigs.PSObject.Properties.Remove($k); $gone += $k
        }
      }
      if (-not $s.pluginConfigs.PSObject.Properties.Name) {
        $s.PSObject.Properties.Remove("pluginConfigs")
      }
    }
    if ($s.PSObject.Properties.Name -contains "enabledPlugins") {
      foreach ($k in @($s.enabledPlugins.PSObject.Properties.Name)) {
        if ($k -match '^(hollerback|agentshare)@') { $s.enabledPlugins.PSObject.Properties.Remove($k) }
      }
    }
    Write-JsonNoBom -Path $SettingsPath -Json ($s | ConvertTo-Json -Depth 20)
    Write-Host "    settings.json: removed $(if ($gone) { $gone -join ', ' } else { 'nothing' })" -ForegroundColor Green
    Write-Host "    (backup: $SettingsPath.bak.hollerback-uninstall)" -ForegroundColor DarkGray
  } catch {
    Write-Host "    settings.json unparseable; leaving it alone" -ForegroundColor Yellow
  }
}

foreach ($p in @("$env:LOCALAPPDATA\hollerback", "$env:USERPROFILE\.cache\hollerback",
                 "$env:USERPROFILE\.cache\agentshare", "$env:USERPROFILE\.hollerback.json",
                 "$env:USERPROFILE\.agentshare.json")) {
  if (Test-Path $p) { Remove-Item -Recurse -Force $p; Write-Host "    removed $p" -ForegroundColor Green; $removed = $true }
}

# Workspace identities (.hollerback\agent.json) are config the installer wrote,
# not data the user created, so they go by default -- otherwise an uninstalled
# machine keeps silently naming sessions. Received files in the same directory
# are the user's and survive unless -PurgeInboxes is given.
if ($PurgeInboxes) {
  Write-Host "    searching for hollerback directories (inboxes included) ..." -ForegroundColor Cyan
  Get-ChildItem -Path $env:USERPROFILE -Recurse -Directory -Force `
      -Include ".hollerback", ".agentshare" -ErrorAction SilentlyContinue -Depth 6 |
    ForEach-Object { Remove-Item -Recurse -Force $_.FullName; Write-Host "      deleted $($_.FullName)" -ForegroundColor DarkGray }
} else {
  Write-Host "    searching for workspace agent names ..." -ForegroundColor Cyan
  $any = $false
  Get-ChildItem -Path $env:USERPROFILE -Recurse -Directory -Force `
      -Include ".hollerback", ".agentshare" -ErrorAction SilentlyContinue -Depth 6 |
    ForEach-Object {
      $f = Join-Path $_.FullName "agent.json"
      if (Test-Path $f) {
        Remove-Item -Force $f
        Write-Host "      removed $f" -ForegroundColor Green
        $any = $true; $removed = $true
      }
    }
  if (-not $any) { Write-Host "      none found" -ForegroundColor DarkGray }
  Write-Host "    keeping received files (.hollerback\inbox\ in your projects)" -ForegroundColor DarkGray
  Write-Host "    re-run with -PurgeInboxes to delete those too" -ForegroundColor DarkGray
}

if (-not $removed) { Write-Host "    nothing was installed" -ForegroundColor Yellow }

Write-Host ""
Write-Host "Done. RESTART any running Claude Code session -- the monitor and MCP" -ForegroundColor Cyan
Write-Host "server are long-lived child processes and keep running until it exits." -ForegroundColor Cyan
