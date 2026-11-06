param([int]$Port = 8792)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) { throw 'Run scripts/setup.ps1 first.' }
if (Test-Path -LiteralPath '.env') {
    foreach ($Line in Get-Content -LiteralPath '.env' -Encoding utf8) {
        if ($Line -match '^\s*([A-Z][A-Z0-9_]*)\s*=(.*)$') {
            $ConfigKey = $Matches[1]
            $ConfigValue = $Matches[2].Trim().Trim('"').Trim("'")
            if ($ConfigKey -match '^(KNOWLEDGE_DB|MODEL_BASE_URL|MODEL_NAME|MODEL_API_KEY|MODEL_TIMEOUT|MODEL_MAX_STEPS|EMBEDDING_BASE_URL|EMBEDDING_NAME|EMBEDDING_API_KEY|EMBEDDING_TIMEOUT|EMBEDDING_MIN_SIMILARITY)$') {
                [Environment]::SetEnvironmentVariable($ConfigKey, $ConfigValue, 'Process')
            }
        }
    }
}
Write-Host "Knowledge Navigator: http://127.0.0.1:$Port (Ctrl+C to stop)"
& '.venv\Scripts\python.exe' -m uvicorn navigator.main:app --host 127.0.0.1 --port $Port
exit $LASTEXITCODE
