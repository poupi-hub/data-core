param(
  [string]$ApiUrl = "http://127.0.0.1:8000"
)

$ErrorActionPreference = "Stop"

Invoke-RestMethod "$ApiUrl/api/v1/operations/collection-readiness" |
  ConvertTo-Json -Depth 14
