# Pre-deployment checks. Run from the project folder:
#   powershell -ExecutionPolicy Bypass -File scripts\predeploy_check.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\predeploy_check.ps1 -WithAnalysisRun
#
# Exit code 0 only if every check passed.
# Note for Windows PowerShell 5.1: a single [PSCustomObject] has no .Count, so failures are
# counted with @(...).Count. (Without @() a lone failure counts as $null and looks like a pass.)
param([switch]$WithAnalysisRun)

$ErrorActionPreference = "Continue"     # Python writes warnings to stderr; judge by exit codes only
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { Write-Host "No .venv found. Run scripts\setup.ps1 first." -ForegroundColor Red; exit 1 }

$results = New-Object System.Collections.Generic.List[object]

function Invoke-Check([string]$Name, [string[]]$PyArgs) {
    Write-Host "`n========== $Name ==========" -ForegroundColor Cyan
    & $python @PyArgs
    $code = $LASTEXITCODE
    $row = [PSCustomObject]@{ Check = $Name; Result = $(if ($code -eq 0) { "PASS" } else { "FAIL (exit $code)" }) }
    $results.Add($row)      # build the object first; [PSCustomObject]@{} inside .Add() comes out empty
}

Invoke-Check "Automated tests (simulated data, fake broker)" @("-m", "pytest", "-q", "-p", "no:warnings", "tests")
Invoke-Check "Configuration, accounts, market clock, MCP" @("main.py", "check")
Invoke-Check "Local MCP client/server with fresh data" @("main.py", "mcp-test", "--prepare")
Invoke-Check "LLM can call tools (needed by the agent)" @("main.py", "llm-test")
if ($WithAnalysisRun) {
    Invoke-Check "Full CrewAI analysis run (no orders)" @("main.py", "run", "--mode", "analysis")
}

Write-Host "`n========== SUMMARY ==========" -ForegroundColor Cyan
foreach ($r in $results) {
    $colour = $(if ($r.Result -eq "PASS") { "Green" } else { "Red" })
    Write-Host ("{0,-52} {1}" -f $r.Check, $r.Result) -ForegroundColor $colour
}
$failed = @($results | Where-Object { $_.Result -ne "PASS" }).Count
if ($failed -gt 0) {
    Write-Host "NOT READY: $failed check(s) failed. Fix them before switching on paper orders." -ForegroundColor Red
    exit 1
}
Write-Host "ALL CHECKS PASSED. Next: one dry_run during market hours, then ENABLE_PAPER_ORDERS=true and paper mode." -ForegroundColor Green
exit 0
