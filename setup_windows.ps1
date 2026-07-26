$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "Creating Python virtual environment..."
py -3 -m venv .venv

Write-Host "Installing Python packages..."
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt

Write-Host "Installing the Playwright Chromium browser..."
& .\.venv\Scripts\python.exe -m playwright install chromium

Write-Host ""
Write-Host "Setup complete. Next, edit run_monitor_windows.ps1 and paste your Discord webhook URL."
