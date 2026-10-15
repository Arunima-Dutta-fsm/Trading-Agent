# Start the daily scheduler. Keep this window open (and the laptop awake) during trading days.
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
& "$root\.venv\Scripts\python.exe" main.py scheduler
