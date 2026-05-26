param(
  [string]$EnvPath = ".env",
  [string]$AlertmanagerUrl = "http://127.0.0.1:9094",
  [string]$ComposeProjectFile = "runtime-data\docker-compose.alertmanager-telegram-smoke.yml"
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

$envValues = Read-DotEnv -Path $EnvPath
$botToken = $envValues["TELEGRAM_BOT_TOKEN"]
$chatId = $envValues["TELEGRAM_CHAT_ID"]

if ([string]::IsNullOrWhiteSpace($botToken)) {
  throw "TELEGRAM_BOT_TOKEN is not configured in $EnvPath"
}

if ([string]::IsNullOrWhiteSpace($chatId)) {
  throw "TELEGRAM_CHAT_ID is not configured in $EnvPath"
}

if ($chatId -notmatch '^-?\d+$') {
  throw "TELEGRAM_CHAT_ID must be numeric for Alertmanager telegram_configs."
}

$previousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& docker.exe info *> $null
$dockerInfoExitCode = $LASTEXITCODE
$ErrorActionPreference = $previousErrorActionPreference
if ($dockerInfoExitCode -ne 0) {
  throw "Docker is not available. Start Docker Desktop and re-run this script."
}

New-Item -ItemType Directory -Force -Path runtime-data | Out-Null

$renderedConfig = Get-Content -LiteralPath "alertmanager\alertmanager.telegram-first.yml" -Raw
$renderedConfig = $renderedConfig.Replace("__TELEGRAM_BOT_TOKEN__", $botToken)
$renderedConfig = $renderedConfig.Replace("__TELEGRAM_CHAT_ID__", $chatId)
$renderedConfig = $renderedConfig.Replace("http://host.docker.internal:9099/alerts", "http://127.0.0.1:65535/disabled-webhook")
$renderedConfigPath = (Resolve-Path -LiteralPath "runtime-data").Path + "\alertmanager-telegram-smoke.yml"
$templatePath = (Resolve-Path -LiteralPath "alertmanager\poupi_telegram.tmpl").Path
Set-Content -Path $renderedConfigPath -Value $renderedConfig -Encoding ASCII

$renderedConfigMount = $renderedConfigPath -replace "\\", "/"
$templateMount = $templatePath -replace "\\", "/"

$composeConfig = @'
services:
  alertmanager-telegram-smoke:
    image: prom/alertmanager:v0.27.0
    ports:
      - "9094:9093"
    volumes:
      - __RENDERED_CONFIG__:/etc/alertmanager/alertmanager.yml:ro
      - __TEMPLATE_FILE__:/etc/alertmanager/templates/poupi_telegram.tmpl:ro
    command:
      - "--config.file=/etc/alertmanager/alertmanager.yml"
      - "--storage.path=/alertmanager"
'@
$composeConfig = $composeConfig.Replace("__RENDERED_CONFIG__", $renderedConfigMount)
$composeConfig = $composeConfig.Replace("__TEMPLATE_FILE__", $templateMount)
Set-Content -Path $ComposeProjectFile -Value $composeConfig -Encoding ASCII

try {
  docker-compose.exe -f $ComposeProjectFile up -d --force-recreate alertmanager-telegram-smoke | Out-Host

  $readyUrl = "$AlertmanagerUrl/-/ready"
  $deadline = (Get-Date).AddSeconds(30)
  do {
    try {
      Invoke-RestMethod -Method Get -Uri $readyUrl | Out-Null
      $ready = $true
    } catch {
      Start-Sleep -Seconds 1
      $ready = $false
    }
  } while (-not $ready -and (Get-Date) -lt $deadline)

  if (-not $ready) {
    throw "Alertmanager telegram smoke service did not become ready at $readyUrl"
  }

  $now = (Get-Date).ToUniversalTime()
  $payload = @"
[
  {
    "labels": {
      "alertname": "PoupiSystemBotSmoke",
      "component": "observability",
      "severity": "critical",
      "channel": "telegram"
    },
    "annotations": {
      "summary": "[SHADOW] Poupi System Bot Alertmanager smoke",
      "impact": "Telegram receiver path validated",
      "action": "No action required",
      "dashboard": "n/a"
    },
    "startsAt": "$($now.ToString("o"))",
    "endsAt": "$($now.AddMinutes(2).ToString("o"))"
  }
]
"@
  Set-Content -Path "runtime-data\alertmanager-telegram-smoke-alert.json" -Value $payload -Encoding ASCII
  curl.exe -sS -X POST "$AlertmanagerUrl/api/v2/alerts" -H "Content-Type: application/json" --data-binary "@runtime-data\alertmanager-telegram-smoke-alert.json" | Out-Null
  Start-Sleep -Seconds 5

  Write-Host "Alertmanager Telegram smoke alert submitted successfully."
  Write-Host "Chat ID: configured"
  Write-Host "Bot token: configured"
} catch {
  $message = $_.Exception.Message -replace [regex]::Escape($botToken), "<redacted>"
  throw "Alertmanager Telegram smoke failed: $message"
} finally {
  docker-compose.exe -f $ComposeProjectFile rm -sf alertmanager-telegram-smoke | Out-Host
}
