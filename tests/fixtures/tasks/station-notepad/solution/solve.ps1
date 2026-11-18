$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path "$env:ALE_HOME\output" | Out-Null
Set-Content -LiteralPath "$env:ALE_HOME\output\result.txt" -Value 'ALE station verified' -NoNewline
