param(
  [string]$Source = "drogasil",
  [int]$Days = 30,
  [int]$Limit = 50,
  [string]$ApiUrl = "http://127.0.0.1:8000"
)

$ErrorActionPreference = "Stop"

Invoke-RestMethod "$ApiUrl/api/v1/sources/ecommerce/$Source/price-changes?days=$Days&limit=$Limit" |
  ConvertTo-Json -Depth 12
