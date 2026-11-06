$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
& '.venv\Scripts\python.exe' -m pip install -r requirements-dev.lock
if ($LASTEXITCODE -ne 0) { throw 'Test dependencies could not be installed.' }
& '.venv\Scripts\python.exe' -m pytest -q
exit $LASTEXITCODE
