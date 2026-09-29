<#
.SYNOPSIS
  Installs the Ondo Quartermaster agent for every person who signs in to this PC.
  Packaged as an Intune Win32 app (see docs/deploy-windows.md); runs as SYSTEM.

.DESCRIPTION
  - Copies the agent (the PyInstaller folder from build.ps1) to Program Files.
  - Writes the configuration to %ProgramData%\Ondo, writable only by administrators.
  - Optionally sets the model gateway key as a machine environment variable.
  - Registers a scheduled task that starts the agent in each person's session at
    sign-in: it needs their desktop, and enrolls as them.
  The control plane address and enrollment token are NOT set here: they come from
  the Ondo ADMX policy, so they can be changed or revoked without reinstalling.

  UNTESTED on a real Intune tenant; see docs/deploy-windows.md.
#>
[CmdletBinding()]
param(
  [string]$Source = $PSScriptRoot,
  [string]$GatewayKey = ""
)
$ErrorActionPreference = "Stop"

$app = Join-Path $env:ProgramFiles "Ondo Quartermaster"
$data = Join-Path $env:ProgramData "Ondo"
$exe = Join-Path $app "ondo-agent.exe"

# 1. The agent.
if (Test-Path $app) { Remove-Item -Recurse -Force $app }
Copy-Item -Recurse -Force (Join-Path $Source "ondo-agent") $app

# 2. Configuration, readable by everyone, writable by administrators and SYSTEM only.
New-Item -ItemType Directory -Force -Path $data | Out-Null
Copy-Item -Force (Join-Path $Source "ondo.yaml") (Join-Path $data "ondo.yaml")
Copy-Item -Force (Join-Path $Source "profiles.yaml") (Join-Path $data "profiles.yaml")
icacls $data /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" "*S-1-5-32-545:(OI)(CI)RX" | Out-Null

# 3. The gateway key, if this deployment passes one.
if ($GatewayKey) {
  [Environment]::SetEnvironmentVariable("ONDO_GATEWAY_KEY", $GatewayKey, "Machine")
}

# 4. Start in each person's session at sign-in, with their rights, not elevated.
$config = Join-Path $data "ondo.yaml"
# Credentials default to the person's own profile (%USERPROFILE%\.ondo\agent.json).
$action = New-ScheduledTaskAction -Execute $exe -Argument "--config `"$config`" connect"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$principal = New-ScheduledTaskPrincipal -GroupId "S-1-5-32-545" -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "Ondo Quartermaster" -TaskPath "\Ondo\" -Action $action -Trigger $trigger `
  -Principal $principal -Settings $settings -Force | Out-Null

# What detect.ps1 checks.
New-Item -Path "HKLM:\SOFTWARE\Ondo\Quartermaster" -Force | Out-Null
$version = (Get-Content (Join-Path $Source "VERSION") -Raw).Trim()
Set-ItemProperty -Path "HKLM:\SOFTWARE\Ondo\Quartermaster" -Name "InstalledVersion" -Value $version
Write-Output "Installed Ondo Quartermaster $version"
