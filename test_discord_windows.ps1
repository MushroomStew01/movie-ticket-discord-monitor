$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$secretsPath = Join-Path $PSScriptRoot "secrets.ps1"
if (Test-Path $secretsPath) {
    . $secretsPath
}
if ([string]::IsNullOrWhiteSpace($env:DISCORD_WEBHOOK_URL)) {
    throw "DISCORD_WEBHOOK_URL is missing. Copy secrets.ps1.example to secrets.ps1 and fill it in."
}

& .\.venv\Scripts\python.exe .\monitor.py --test-alert
exit $LASTEXITCODE
