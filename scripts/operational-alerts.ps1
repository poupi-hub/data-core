param(
  [string]$Module,
  [string]$Source,
  [int]$RawFreshnessHours = 24,
  [int]$RawPendingMinutes = 60,
  [int]$AnalyticsPendingMinutes = 120,
  [int]$Limit = 50,
  [string]$ApiUrl = "http://127.0.0.1:8000"
)

$ErrorActionPreference = "Stop"

$query = @(
  "raw_freshness_hours=$RawFreshnessHours",
  "raw_pending_minutes=$RawPendingMinutes",
  "analytics_pending_minutes=$AnalyticsPendingMinutes",
  "limit=$Limit"
)
if ($Module) { $query += "module=$([uri]::EscapeDataString($Module))" }
if ($Source) { $query += "source_name=$([uri]::EscapeDataString($Source))" }

$url = "$ApiUrl/api/v1/operations/alerts?$($query -join '&')"
Invoke-RestMethod $url | ConvertTo-Json -Depth 12
