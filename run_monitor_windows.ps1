$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# REQUIRED: Paste the complete Discord-generated webhook URL between the quotes.
# Example format only:
# https://discord.com/api/webhooks/123456789012345678/long_private_token_here
$env:DISCORD_WEBHOOK_URL = "PASTE_YOUR_DISCORD_WEBHOOK_URL_HERE"

# OPTIONAL BUT RECOMMENDED: Paste your numeric Discord User ID to make every alert mention you.
# Leave this blank if you prefer normal channel notifications.
$env:DISCORD_USER_ID = ""

& .\.venv\Scripts\python.exe .\monitor.py
exit $LASTEXITCODE
