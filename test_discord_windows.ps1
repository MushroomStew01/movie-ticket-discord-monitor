$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$env:DISCORD_WEBHOOK_URL = "PASTE_YOUR_DISCORD_WEBHOOK_URL_HERE"
$env:DISCORD_USER_ID = ""

& .\.venv\Scripts\python.exe .\monitor.py --test-alert
exit $LASTEXITCODE
