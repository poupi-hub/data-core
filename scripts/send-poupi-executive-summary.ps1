param(
  [string]$EnvPath = ".env",
  [string]$PrometheusUrl = "http://127.0.0.1:9090",
  [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function Read-DotEnv {
  param([string]$Path)

  if (-not (Test-Path -LiteralPath $Path)) {
    throw "Env file not found: $Path"
  }

  $values = @{}
  Get-Content -LiteralPath $Path | ForEach-Object {
    $line = $_.Trim()
    if ($line.Length -eq 0 -or $line.StartsWith("#")) {
      return
    }
    $parts = $line -split "=", 2
    if ($parts.Count -ne 2) {
      return
    }
    $key = $parts[0].Trim()
    $value = $parts[1].Trim().Trim('"').Trim("'")
    $values[$key] = $value
  }

  return $values
}

function Query-PrometheusValue {
  param(
    [string]$BaseUrl,
    [string]$Query
  )

  $encoded = [System.Uri]::EscapeDataString($Query)
  $url = "$BaseUrl/api/v1/query?query=$encoded"

  try {
    $response = Invoke-RestMethod -Method Get -Uri $url -TimeoutSec 10
    if ($response.status -ne "success" -or $response.data.result.Count -eq 0) {
      return $null
    }
    return [double]$response.data.result[0].value[1]
  } catch {
    return $null
  }
}

function Format-Status {
  param(
    [string]$Name,
    [Nullable[double]]$Value,
    [scriptblock]$Formatter
  )

  if ($null -eq $Value) {
    return "$Name UNKNOWN"
  }

  return "$Name $(& $Formatter $Value)"
}

function Send-TelegramMessage {
  param(
    [string]$BotToken,
    [string]$ChatId,
    [string]$Text
  )

  $uri = "https://api.telegram.org/bot$BotToken/sendMessage"
  $body = @{
    chat_id = $ChatId
    parse_mode = "HTML"
    text = $Text
  }
  Invoke-RestMethod -Method Post -Uri $uri -Body $body | Out-Null
}

$envValues = Read-DotEnv -Path $EnvPath
$botToken = $envValues["TELEGRAM_BOT_TOKEN"]
$chatId = $envValues["TELEGRAM_CHAT_ID"]

if ([string]::IsNullOrWhiteSpace($botToken)) {
  throw "TELEGRAM_BOT_TOKEN is not configured in $EnvPath"
}

if ([string]::IsNullOrWhiteSpace($chatId)) {
  throw "TELEGRAM_CHAT_ID is not configured in $EnvPath"
}

$checks = [ordered]@{
  api_up = Query-PrometheusValue -BaseUrl $PrometheusUrl -Query 'up{job="data-core-api"}'
  scheduler_heartbeat_age = Query-PrometheusValue -BaseUrl $PrometheusUrl -Query 'scheduler_heartbeat_age_seconds'
  scheduler_restarts_15m = Query-PrometheusValue -BaseUrl $PrometheusUrl -Query 'increase(data_core_scheduler_restart_count[15m])'
  scheduler_oom_state = Query-PrometheusValue -BaseUrl $PrometheusUrl -Query 'data_core_scheduler_state == 4'
  normalization_lag = Query-PrometheusValue -BaseUrl $PrometheusUrl -Query 'pipeline_liveness_lag_seconds{pipeline_id="normalize_ecommerce"}'
  queue_lag = Query-PrometheusValue -BaseUrl $PrometheusUrl -Query 'queue_lag_seconds{module="ecommerce"}'
  volatile_redis = Query-PrometheusValue -BaseUrl $PrometheusUrl -Query 'redis_up{job="poupi-crypto-volatile"}'
  volatile_oom = Query-PrometheusValue -BaseUrl $PrometheusUrl -Query 'container_oom_killed_total{job="poupi-crypto-volatile"}'
  active_critical_alerts = Query-PrometheusValue -BaseUrl $PrometheusUrl -Query 'count(ALERTS{alertstate="firing",severity="critical"}) or vector(0)'
  active_warning_alerts = Query-PrometheusValue -BaseUrl $PrometheusUrl -Query 'count(ALERTS{alertstate="firing",severity="warning"}) or vector(0)'
}

$criticalReasons = @()
$degradedReasons = @()

if ($checks.api_up -ne $null -and $checks.api_up -lt 1) { $criticalReasons += "api_down" }
if ($checks.scheduler_heartbeat_age -ne $null -and ($checks.scheduler_heartbeat_age -gt 600 -or $checks.scheduler_heartbeat_age -eq -1)) { $criticalReasons += "scheduler_stale" }
if ($checks.scheduler_restarts_15m -ne $null -and $checks.scheduler_restarts_15m -gt 0) { $criticalReasons += "scheduler_restart" }
if ($checks.scheduler_oom_state -ne $null -and $checks.scheduler_oom_state -gt 0) { $criticalReasons += "scheduler_oom" }
if ($checks.normalization_lag -ne $null -and $checks.normalization_lag -gt 7200) { $criticalReasons += "normalization_lag" }
if ($checks.queue_lag -ne $null -and $checks.queue_lag -gt 14400) { $criticalReasons += "queue_lag" }
if ($checks.volatile_oom -ne $null -and $checks.volatile_oom -gt 0) { $criticalReasons += "volatile_oom" }
if ($checks.active_critical_alerts -ne $null -and $checks.active_critical_alerts -gt 0) { $criticalReasons += "critical_alerts" }

if ($checks.scheduler_heartbeat_age -ne $null -and $checks.scheduler_heartbeat_age -gt 300 -and $checks.scheduler_heartbeat_age -le 600) { $degradedReasons += "scheduler_heartbeat" }
if ($checks.volatile_redis -ne $null -and $checks.volatile_redis -lt 1) { $degradedReasons += "volatile_redis" }
if ($checks.active_warning_alerts -ne $null -and $checks.active_warning_alerts -gt 0) { $degradedReasons += "warning_alerts" }

$globalStatus = "OK"
if ($criticalReasons.Count -gt 0) {
  $globalStatus = "CRITICAL"
} elseif ($degradedReasons.Count -gt 0) {
  $globalStatus = "DEGRADED"
}

$timestamp = (Get-Date).ToString("yyyy-MM-dd HH:mm")
$action = "none"
if ($globalStatus -eq "CRITICAL") {
  $action = "check critical reasons: " + ($criticalReasons -join ", ")
} elseif ($globalStatus -eq "DEGRADED") {
  $action = "watch degraded reasons: " + ($degradedReasons -join ", ")
}

$lines = @(
  "<b>Poupi Executive Summary - $timestamp</b>",
  "Status: $globalStatus",
  "",
  (Format-Status -Name "API:" -Value $checks.api_up -Formatter { param($v) if ($v -ge 1) { "OK" } else { "DOWN" } }),
  (Format-Status -Name "Scheduler heartbeat:" -Value $checks.scheduler_heartbeat_age -Formatter { param($v) if ($v -lt 0) { "missing" } else { "$([int]$v)s" } }),
  (Format-Status -Name "Scheduler restarts 15m:" -Value $checks.scheduler_restarts_15m -Formatter { param($v) "$([int]$v)" }),
  (Format-Status -Name "Normalization lag:" -Value $checks.normalization_lag -Formatter { param($v) "$([int]($v / 60))m" }),
  (Format-Status -Name "Queue lag:" -Value $checks.queue_lag -Formatter { param($v) "$([int]($v / 60))m" }),
  (Format-Status -Name "Volatile Redis:" -Value $checks.volatile_redis -Formatter { param($v) if ($v -ge 1) { "OK" } else { "DOWN" } }),
  "Active alerts: critical=$(if ($checks.active_critical_alerts -ne $null) { [int]$checks.active_critical_alerts } else { 'UNKNOWN' }) warning=$(if ($checks.active_warning_alerts -ne $null) { [int]$checks.active_warning_alerts } else { 'UNKNOWN' })",
  "Action: $action"
)

$message = $lines -join "`n"

if ($DryRun) {
  Write-Host $message
  exit 0
}

try {
  Send-TelegramMessage -BotToken $botToken -ChatId $chatId -Text $message
  Write-Host "Poupi executive summary delivered successfully."
} catch {
  $errorMessage = $_.Exception.Message -replace [regex]::Escape($botToken), "<redacted>"
  throw "Poupi executive summary failed: $errorMessage"
}
