param(
  [string]$TargetsPath = ".\examples\poupi-baby-targets.json",
  [string]$ApiUrl = "http://127.0.0.1:8000",
  [string]$Source,
  [string]$CollectorName = "poupi_legacy_raw_collector",
  [int]$Limit = 100,
  [int]$MaxTargets = 0,
  [double]$DelaySeconds = 0,
  [int]$TimeoutSeconds = 0,
  [double]$MinActiveReadinessRate = 1.0,
  [switch]$SkipImport,
  [switch]$SkipCollect,
  [switch]$SkipExports
)

$ErrorActionPreference = "Stop"

function Invoke-JsonScript {
  param([string[]]$Arguments)

  $output = powershell @Arguments
  $text = $output | Out-String
  $jsonStart = $text.IndexOf("{")
  $jsonEnd = $text.LastIndexOf("}")
  if ($jsonStart -lt 0 -or $jsonEnd -lt $jsonStart) {
    throw "Script did not return a JSON object: $text"
  }
  return $text.Substring($jsonStart, $jsonEnd - $jsonStart + 1) | ConvertFrom-Json
}

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

function Build-QualityQuery {
  $query = @(
    "module=ecommerce",
    "collector_name=$([uri]::EscapeDataString($CollectorName))"
  )
  if ($Source) { $query += "source_name=$([uri]::EscapeDataString($Source))" }
  return "?$($query -join '&')"
}

Wait-ApiHealth -Url $ApiUrl

$importResult = $null
if (-not $SkipImport) {
  Write-Host "Importing Poupi Baby targets..."
  $importResult = Invoke-JsonScript @(
    "-ExecutionPolicy", "Bypass",
    "-File", ".\scripts\import-targets.ps1",
    "-Path", $TargetsPath,
    "-ApiUrl", $ApiUrl
  )

  if ($importResult.errors -and $importResult.errors.Count -gt 0) {
    throw "Target import returned $($importResult.errors.Count) validation error(s)"
  }
}

Write-Host "Running Poupi Baby pipeline validation..."
$validateArgs = @(
  "-ExecutionPolicy", "Bypass",
  "-File", ".\scripts\validate-collection.ps1",
  "-Module", "ecommerce",
  "-CollectorName", $CollectorName,
  "-Limit", $Limit,
  "-ApiUrl", $ApiUrl
)
if ($Source) { $validateArgs += @("-Source", $Source) }
if ($MaxTargets -gt 0) { $validateArgs += @("-MaxTargets", $MaxTargets) }
if ($DelaySeconds -gt 0) { $validateArgs += @("-DelaySeconds", $DelaySeconds) }
if ($TimeoutSeconds -gt 0) { $validateArgs += @("-TimeoutSeconds", $TimeoutSeconds) }
if ($SkipCollect) { $validateArgs += "-SkipCollect" }
if ($SkipExports) { $validateArgs += "-SkipExports" }
$validation = Invoke-JsonScript $validateArgs

Write-Host "Checking Poupi Baby source quality..."
$quality = Invoke-RestMethod "$ApiUrl/api/v1/operations/source-quality$(Build-QualityQuery)"
$activeProblemSources = @(
  $quality.sources | Where-Object {
    $_.active_target_count -gt 0 -and $_.health_status -ne "ok"
  }
)

if ($quality.summary.active_readiness_rate -lt $MinActiveReadinessRate) {
  throw "Active readiness rate $($quality.summary.active_readiness_rate) is below required $MinActiveReadinessRate"
}

if ($quality.summary.blocked_active_target_count -gt 0) {
  throw "Source quality has $($quality.summary.blocked_active_target_count) blocked active target(s)"
}

if ($activeProblemSources.Count -gt 0) {
  $names = ($activeProblemSources | ForEach-Object { "$($_.source_name):$($_.health_status)" }) -join ", "
  throw "Active sources with non-ok health: $names"
}

$standbySources = @($quality.sources | Where-Object { $_.health_status -eq "standby" } | Select-Object -ExpandProperty source_name)

[pscustomobject]@{
  ok = $true
  imported = if ($importResult) {
    [pscustomobject]@{
      created = $importResult.created
      updated = $importResult.updated
      skipped = $importResult.skipped
      total = $importResult.total
    }
  } else {
    $null
  }
  validation = $validation
  source_quality = [pscustomobject]@{
    active_readiness_rate = $quality.summary.active_readiness_rate
    raw_to_normalized_rate = $quality.summary.raw_to_normalized_rate
    normalized_to_analytics_rate = $quality.summary.normalized_to_analytics_rate
    blocked_active_target_count = $quality.summary.blocked_active_target_count
    standby_sources = $standbySources
  }
} | ConvertTo-Json -Depth 12
