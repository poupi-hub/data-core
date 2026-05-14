param(
  [string]$Module = "ecommerce",
  [string]$Source,
  [string]$CollectorName = "poupi_legacy_raw_collector",
  [int]$Limit = 100,
  [int]$MaxTargets = 0,
  [double]$DelaySeconds = 0,
  [int]$TimeoutSeconds = 0,
  [string]$ApiUrl = "http://127.0.0.1:8000",
  [switch]$SkipCollect,
  [switch]$SkipExports
)

$ErrorActionPreference = "Stop"

function Wait-ApiHealth {
  param([string]$Url, [int]$TimeoutSeconds = 60)

  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  do {
    try {
      $health = Invoke-RestMethod "$Url/health"
      if ($health.status -eq "ok") {
        return
      }
    } catch {
      Start-Sleep -Seconds 2
    }
  } while ((Get-Date) -lt $deadline)

  throw "API health check did not become ready within $TimeoutSeconds seconds"
}

function Build-CoverageQuery {
  $query = @()
  if ($Module) { $query += "module=$([uri]::EscapeDataString($Module))" }
  if ($Source) { $query += "source_name=$([uri]::EscapeDataString($Source))" }
  if ($CollectorName) { $query += "collector_name=$([uri]::EscapeDataString($CollectorName))" }
  if ($query.Count -eq 0) { return "" }
  return "?$($query -join '&')"
}

Wait-ApiHealth -Url $ApiUrl

if (-not $SkipCollect) {
  Write-Host "Running collection targets..."
  $collectArgs = @("-ExecutionPolicy", "Bypass", "-File", ".\scripts\run-targets.ps1")
  if ($Module) { $collectArgs += @("-Module", $Module) }
  if ($Source) { $collectArgs += @("-Source", $Source) }
  if ($CollectorName) { $collectArgs += @("-CollectorName", $CollectorName) }
  $collectArgs += @("-Limit", $Limit)
  if ($MaxTargets -gt 0) { $collectArgs += @("-MaxTargets", $MaxTargets) }
  if ($DelaySeconds -gt 0) { $collectArgs += @("-DelaySeconds", $DelaySeconds) }
  if ($TimeoutSeconds -gt 0) { $collectArgs += @("-TimeoutSeconds", $TimeoutSeconds) }
  $collectArgs += @("-ApiUrl", $ApiUrl)
  powershell @collectArgs
}

Write-Host "Running normalization and analytics worker once..."
$pipelineQuery = @("limit=100")
if ($Module) { $pipelineQuery += "module=$([uri]::EscapeDataString($Module))" }
Invoke-RestMethod -Method Post "$ApiUrl/api/v1/operations/pipeline/run?$($pipelineQuery -join '&')" | Out-Null

Write-Host "Checking collection readiness..."
$readiness = Invoke-RestMethod "$ApiUrl/api/v1/operations/collection-readiness"

Write-Host "Checking collection coverage..."
$coverageQuery = Build-CoverageQuery
$coverage = Invoke-RestMethod "$ApiUrl/api/v1/operations/collection-coverage$coverageQuery"

$markdownOutput = $null
$csvOutput = $null
if (-not $SkipExports) {
  Write-Host "Exporting coverage reports..."
  $exportArgs = @("-ExecutionPolicy", "Bypass", "-File", ".\scripts\export-collection-coverage.ps1")
  if ($Module) { $exportArgs += @("-Module", $Module) }
  if ($Source) { $exportArgs += @("-Source", $Source) }
  if ($CollectorName) { $exportArgs += @("-CollectorName", $CollectorName) }
  $exportArgs += @("-ApiUrl", $ApiUrl)
  $markdownOutput = powershell @($exportArgs + @("-Format", "markdown")) | ConvertFrom-Json
  $csvOutput = powershell @($exportArgs + @("-Format", "csv")) | ConvertFrom-Json
}

$result = [pscustomobject]@{
  ready = $readiness.ready
  readiness = [pscustomobject]@{
    target_count = $readiness.target_count
    ready_target_count = $readiness.ready_target_count
    blocking_target_count = $readiness.blocking_target_count
    raw_pending = $readiness.raw_pending
    raw_failed = $readiness.raw_failed
    analytics_pending = $readiness.analytics_pending
    unresolved_collector_errors = $readiness.unresolved_collector_errors
  }
  coverage = $coverage.summary
  reports = [pscustomobject]@{
    markdown = if ($markdownOutput) { $markdownOutput.output_path } else { $null }
    csv = if ($csvOutput) { $csvOutput.output_path } else { $null }
  }
}

$result | ConvertTo-Json -Depth 8

if (-not $readiness.ready) {
  throw "Collection readiness failed: $($readiness.ready_target_count)/$($readiness.target_count) active targets ready"
}

if ($coverage.summary.blocked_active_target_count -gt 0) {
  throw "Collection coverage has $($coverage.summary.blocked_active_target_count) blocked active target(s)"
}
