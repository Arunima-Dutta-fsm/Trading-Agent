# One-time setup on a new Windows laptop. Run from the project folder:
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "1/5 Checking for Python 3.11..."
$py = $null
try { & py -3.11 --version *> $null; if ($LASTEXITCODE -eq 0) { $py = @("py", "-3.11") } } catch {}
if (-not $py) {
    try { $v = & python --version 2>&1; if ($v -match "3\.1[12]") { $py = @("python") } } catch {}
}
if (-not $py) {
    Write-Host "Python 3.11 not found. Install it from https://www.python.org/downloads/release/python-3119/"
    Write-Host "(tick 'Add python.exe to PATH'), or run:  winget install -e --id Python.Python.3.11"
    exit 1
}

Write-Host "2/5 Creating a fresh virtual environment in .venv ..."
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    if ($py.Count -gt 1) { & $py[0] $py[1] -m venv .venv } else { & $py[0] -m venv .venv }
}
$vpy = Join-Path $root ".venv\Scripts\python.exe"

Write-Host "3/5 Installing packages (several minutes the first time)..."
& $vpy -m pip install --upgrade pip
& $vpy -m pip install -r requirements.txt

Write-Host "4/5 Creating .env from .env.example if it does not exist..."
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env"; Write-Host "   Created .env - open it and fill in your keys and PINs." }
else { Write-Host "   .env already exists - left unchanged." }

Write-Host "5/5 Creating folders and the database..."
& $vpy main.py init
Write-Host ""
Write-Host "Done. Next: edit .env, then run:  .venv\Scripts\python.exe main.py check"
