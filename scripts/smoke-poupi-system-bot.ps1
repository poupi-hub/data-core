param(
  [string]$EnvPath = ".env",
  [string]$Text = "<b>TEST - Poupi System Bot</b>`nStatus: Telegram receiver OK"
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

$uri = "https://api.telegram.org/bot$botToken/sendMessage"
$body = @{
  chat_id = $chatId
  parse_mode = "HTML"
  text = $Text
}

try {
  $response = Invoke-RestMethod -Method Post -Uri $uri -Body $body
  if ($response.ok -ne $true) {
    throw "Telegram API returned ok=false"
  }
  Write-Host "Poupi System Bot smoke message delivered successfully."
  Write-Host "Chat ID: configured"
  Write-Host "Bot token: configured"
} catch {
  $message = $_.Exception.Message -replace [regex]::Escape($botToken), "<redacted>"
  throw "Poupi System Bot smoke message failed: $message"
}
