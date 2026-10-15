# Optional: open the dashboard on your phone through ngrok, protected by a username and password.
# Needs ngrok installed and signed in once (ngrok config add-authtoken <token>).
param([string]$User = "aaba", [Parameter(Mandatory = $true)][string]$Password, [int]$Port = 8501)
if ($Password.Length -lt 8) { Write-Host "Use a password of at least 8 characters."; exit 1 }
ngrok http $Port --basic-auth "$($User):$($Password)"
