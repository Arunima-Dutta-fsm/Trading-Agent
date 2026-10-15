# One safe analysis run (no orders, no approvals). Add -NoLlm to skip the LLM.
param([switch]$NoLlm)
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$cmdArgs = @("main.py", "run", "--mode", "analysis")
if ($NoLlm) { $cmdArgs += "--no-llm" }
& "$root\.venv\Scripts\python.exe" @args
