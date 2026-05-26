param(
  [string]$EnvPath = ".env",
  [string]$TemplatePath = "alertmanager\alertmanager.telegram-first.yml",
  [string]$OutputPath = "runtime-data\alertmanager.telegram-first.rendered.yml"
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

New-Item -ItemType Directory -Force -Path (Split-Path -Path $OutputPath -Parent) | Out-Null

$renderedConfig = Get-Content -LiteralPath $TemplatePath -Raw
$renderedConfig = $renderedConfig.Replace("__TELEGRAM_BOT_TOKEN__", $botToken)
$renderedConfig = $renderedConfig.Replace("__TELEGRAM_CHAT_ID__", $chatId)

Set-Content -Path $OutputPath -Value $renderedConfig -Encoding ASCII

Write-Host "Rendered Alertmanager Telegram-first config:"
Write-Host $OutputPath
Write-Host "Bot token: configured"
Write-Host "Chat ID: configured"
