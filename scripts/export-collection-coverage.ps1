param(
  [ValidateSet("markdown", "csv")]
  [string]$Format = "markdown",

  [string]$Module,
  [string]$Source,
  [string]$CollectorName,
  [Nullable[bool]]$Active,
  [string]$OutputPath,
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

$coverage = Invoke-RestMethod $url

if (-not $OutputPath) {
  $extension = if ($Format -eq "csv") { "csv" } else { "md" }
  $OutputPath = Join-Path (Join-Path (Get-Location) "runtime-data") "collection-coverage.$extension"
}

$resolvedDirectory = Split-Path -Parent $OutputPath
if ($resolvedDirectory) {
  New-Item -ItemType Directory -Force -Path $resolvedDirectory | Out-Null
}

function Join-Issues($issues) {
  $items = @($issues)
  if ($items.Count -eq 0) { return "" }
  return ($items -join "; ")
}

function Escape-MarkdownCell($value) {
  if ($null -eq $value) { return "" }
  return ([string]$value).Replace("|", "\|").Replace("`r", " ").Replace("`n", " ")
}

if ($Format -eq "csv") {
  $rows = @($coverage.targets) | ForEach-Object {
    [pscustomobject]@{
      module = $_.target.module
      source_name = $_.target.source_name
      status = $_.status
      active = $_.target.active
      ready = $_.ready
      target_url = $_.target.target_url
      product_seed = $_.target.metadata_json.product_seed
      category = $_.target.metadata_json.category
      freshness_status = $_.freshness.status
      latest_collected_at = $_.freshness.latest_collected_at
      latest_raw_status = if ($_.latest_raw) { $_.latest_raw.processing_status } else { "" }
      normalized_count = $_.normalized_count
      analytics_count = $_.analytics_count
      issues = Join-Issues $_.issues
    }
  }
  $rows | Export-Csv -LiteralPath $OutputPath -NoTypeInformation -Encoding UTF8
} else {
  $lines = New-Object System.Collections.Generic.List[string]
  $generatedAt = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
  $lines.Add("# Collection Coverage")
  $lines.Add("")
  $lines.Add("Generated at: ``$generatedAt``")
  $lines.Add("")
  $lines.Add("## Summary")
  $lines.Add("")
  $lines.Add("| Metric | Value |")
  $lines.Add("| --- | ---: |")
  $lines.Add("| Targets | $($coverage.summary.target_count) |")
  $lines.Add("| Active targets | $($coverage.summary.active_target_count) |")
  $lines.Add("| Ready active targets | $($coverage.summary.ready_active_target_count) |")
  $lines.Add("| Candidate targets | $($coverage.summary.candidate_target_count) |")
  $lines.Add("| Blocked active targets | $($coverage.summary.blocked_active_target_count) |")
  $lines.Add("| RAW records | $($coverage.summary.raw_count) |")
  $lines.Add("| Normalized records | $($coverage.summary.normalized_count) |")
  $lines.Add("| Analytics records | $($coverage.summary.analytics_count) |")
  $lines.Add("")
  $lines.Add("## Sources")
  $lines.Add("")
  $lines.Add("| Module | Source | Targets | Active | Ready | Candidates | RAW | Normalized | Analytics | Issues |")
  $lines.Add("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |")
  foreach ($sourceItem in @($coverage.sources)) {
    $lines.Add("| $(Escape-MarkdownCell $sourceItem.module) | $(Escape-MarkdownCell $sourceItem.source_name) | $($sourceItem.target_count) | $($sourceItem.active_target_count) | $($sourceItem.ready_target_count) | $($sourceItem.candidate_target_count) | $($sourceItem.raw_count) | $($sourceItem.normalized_count) | $($sourceItem.analytics_count) | $(Escape-MarkdownCell (Join-Issues $sourceItem.issues)) |")
  }
  $lines.Add("")
  $lines.Add("## Targets")
  $lines.Add("")
  $lines.Add("| Status | Source | Active | Ready | Product seed | Freshness | RAW status | Normalized | Analytics | Issues | URL |")
  $lines.Add("| --- | --- | --- | --- | --- | --- | --- | ---: | ---: | --- | --- |")
  foreach ($targetItem in @($coverage.targets)) {
    $rawStatus = if ($targetItem.latest_raw) { $targetItem.latest_raw.processing_status } else { "" }
    $lines.Add("| $(Escape-MarkdownCell $targetItem.status) | $(Escape-MarkdownCell $targetItem.target.source_name) | $($targetItem.target.active) | $($targetItem.ready) | $(Escape-MarkdownCell $targetItem.target.metadata_json.product_seed) | $(Escape-MarkdownCell $targetItem.freshness.status) | $(Escape-MarkdownCell $rawStatus) | $($targetItem.normalized_count) | $($targetItem.analytics_count) | $(Escape-MarkdownCell (Join-Issues $targetItem.issues)) | $(Escape-MarkdownCell $targetItem.target.target_url) |")
  }
  $lines | Set-Content -LiteralPath $OutputPath -Encoding UTF8
}

[pscustomobject]@{
  format = $Format
  output_path = (Resolve-Path -LiteralPath $OutputPath).Path
  target_count = $coverage.summary.target_count
  active_target_count = $coverage.summary.active_target_count
  ready_active_target_count = $coverage.summary.ready_active_target_count
  candidate_target_count = $coverage.summary.candidate_target_count
  blocked_active_target_count = $coverage.summary.blocked_active_target_count
} | ConvertTo-Json -Depth 4
