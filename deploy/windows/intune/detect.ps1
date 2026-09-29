<#
.SYNOPSIS
  Intune detection rule: installed when the agent is present at the expected version.
  Intune treats output on stdout with exit code 0 as "installed". UNTESTED on a real tenant.
#>
$expected = "0.1.0"
$exe = Join-Path $env:ProgramFiles "Ondo Quartermaster\ondo-agent.exe"
$installed = (Get-ItemProperty -Path "HKLM:\SOFTWARE\Ondo\Quartermaster" -Name "InstalledVersion" -ErrorAction SilentlyContinue).InstalledVersion
if ((Test-Path $exe) -and ($installed -eq $expected)) {
  Write-Output "Ondo Quartermaster $installed"
  exit 0
}
exit 1
