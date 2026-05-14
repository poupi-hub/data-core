param(
  [string]$Module,
  [string]$Source,
  [string]$CollectorName,
  [int]$Limit = 100,
  [int]$MaxTargets = 0,
  [double]$DelaySeconds = 0,
  [int]$TimeoutSeconds = 0,
  [switch]$DryRun,
  [switch]$ListOnly,
  [string]$ApiUrl = "http://127.0.0.1:8000"
)

$ErrorActionPreference = "Stop"

$query = @("limit=$Limit")
if ($Module) { $query += "module=$([uri]::EscapeDataString($Module))" }
if ($Source) { $query += "source_name=$([uri]::EscapeDataString($Source))" }
if ($CollectorName) { $query += "collector_name=$([uri]::EscapeDataString($CollectorName))" }
if ($MaxTargets -gt 0) { $query += "max_targets=$MaxTargets" }
if ($DelaySeconds -gt 0) { $query += "delay_seconds=$DelaySeconds" }
if ($TimeoutSeconds -gt 0) { $query += "timeout_seconds=$TimeoutSeconds" }
if ($DryRun) { $query += "dry_run=true" }
if ($ListOnly) { $query += "list_only=true" }

$url = "$ApiUrl/api/v1/collection-targets/run?$($query -join '&')"
Invoke-RestMethod -Method Post $url | ConvertTo-Json -Depth 12
