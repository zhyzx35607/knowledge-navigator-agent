$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 is required. Install it and retry.' }
}
& '.venv\Scripts\python.exe' -m ensurepip --upgrade
if ($LASTEXITCODE -ne 0) { throw 'pip bootstrap failed.' }
& '.venv\Scripts\python.exe' -m pip install -r requirements.lock
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& '.venv\Scripts\python.exe' -m pip install --no-deps -e .
if ($LASTEXITCODE -ne 0) { throw 'Project installation failed.' }
Write-Host 'Ready. Run: powershell -File scripts/start.ps1'
