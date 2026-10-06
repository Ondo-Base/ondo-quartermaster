<#
.SYNOPSIS
  Removes the Ondo Quartermaster agent. Each person's runs, consents and credentials
  in %LOCALAPPDATA%\Ondo are left in place (they are that person's record); revoke
  the agents in the admin console to stop them connecting. UNTESTED on a real tenant.
#>
$ErrorActionPreference = "Continue"
Get-Process -Name "ondo-agent" -ErrorAction SilentlyContinue | Stop-Process -Force
Unregister-ScheduledTask -TaskName "Ondo Quartermaster" -TaskPath "\Ondo\" -Confirm:$false -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force (Join-Path $env:ProgramFiles "Ondo Quartermaster") -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force (Join-Path $env:ProgramData "Ondo") -ErrorAction SilentlyContinue
Remove-Item -Path "HKLM:\SOFTWARE\Ondo\Quartermaster" -Recurse -Force -ErrorAction SilentlyContinue
[Environment]::SetEnvironmentVariable("ONDO_GATEWAY_KEY", $null, "Machine")
Write-Output "Removed Ondo Quartermaster"
exit 0
