# Start the web dashboard at http://localhost:8501
param([int]$Port = 8501)
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
& "$root\.venv\Scripts\python.exe" main.py dashboard --port $Port
