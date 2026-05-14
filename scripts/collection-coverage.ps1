param(
  [string]$Module,
  [string]$Source,
  [string]$CollectorName,
  [Nullable[bool]]$Active,
  [string]$ApiUrl = "http://127.0.0.1:8000"
)

$ErrorActionPreference = "Stop"

$query = @()
if ($Module) { $query += "module=$([uri]::EscapeDataString($Module))" }
if ($Source) { $query += "source_name=$([uri]::EscapeDataString($Source))" }
if ($CollectorName) { $query += "collector_name=$([uri]::EscapeDataString($CollectorName))" }
if ($null -ne $Active) { $query += "active=$($Active.ToString().ToLowerInvariant())" }

$url = "$ApiUrl/api/v1/operations/collection-coverage"
if ($query.Count -gt 0) {
  $url = "$url`?$($query -join '&')"
}

Invoke-RestMethod $url | ConvertTo-Json -Depth 14
