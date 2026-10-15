# Run the automated tests (simulated data, fake broker, no network, no orders).
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
& "$root\.venv\Scripts\python.exe" -m pytest -q tests
