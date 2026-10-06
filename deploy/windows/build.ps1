<#
.SYNOPSIS
  Builds the folder an Intune Win32 app is made from: the agent as a standalone
  executable (PyInstaller, one folder), its configuration, and the install,
  detect and uninstall scripts.

.DESCRIPTION
  Run on Windows from the repository root, with Python 3.12:
    powershell -File deploy\windows\build.ps1
  Then wrap it for Intune with Microsoft's Win32 Content Prep Tool:
    IntuneWinAppUtil.exe -c out\intune -s install.ps1 -o out
  The windows workflow (.github/workflows/windows.yml) runs this and a smoke
  install on a GitHub-hosted Windows runner when started by hand.
#>
[CmdletBinding()]
param([string]$Out = "out\intune")
$ErrorActionPreference = "Stop"

python -m pip install --upgrade pip
python -m pip install -e "agent[desktop]" "pyinstaller>=6.10"

$work = "out\build"
New-Item -ItemType Directory -Force -Path $work, $Out | Out-Null
Set-Content -Path "$work\entry.py" -Value "from ondo_agent.cli import main`nmain()`n"

# One folder, not one file: it starts faster and antivirus treats it better.
python -m PyInstaller --noconfirm --clean --onedir --name ondo-agent `
  --distpath $Out --workpath "$work\pyi" --specpath $work `
  --collect-submodules ondo_agent --collect-data ondo_agent `
  --collect-all mcp --collect-submodules pywinauto --collect-submodules comtypes `
  "$work\entry.py"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

Copy-Item -Force deploy\windows\ondo.yaml, agent\config\profiles.yaml $Out
Copy-Item -Force deploy\windows\intune\*.ps1 $Out
$version = (Select-String -Path agent\pyproject.toml -Pattern '^version = "(.+)"').Matches[0].Groups[1].Value
Set-Content -Path "$Out\VERSION" -Value $version -NoNewline

# The executable starts and knows its tools: the smallest proof it was built whole.
& "$Out\ondo-agent\ondo-agent.exe" tools-doc | Out-Null
if ($LASTEXITCODE -ne 0) { throw "the built agent does not start" }
Write-Output "Built $Out (version $version)"
