param(
  [string]$AlertmanagerUrl = "http://127.0.0.1:9094",
  [string]$ComposeProjectFile = "runtime-data\docker-compose.alertmanager-smoke.yml"
)

$ErrorActionPreference = "Stop"

New-Item -ItemType Directory -Force -Path runtime-data | Out-Null

$alertmanagerConfig = @'
global:
  resolve_timeout: 5m

route:
  receiver: smoke-webhook
  group_wait: 1s
  group_interval: 1s
  repeat_interval: 5m

receivers:
  - name: smoke-webhook
    webhook_configs:
      - url: http://alert-webhook-smoke:9099/alerts
        send_resolved: true
'@
Set-Content -Path runtime-data\alertmanager-smoke.yml -Value $alertmanagerConfig -Encoding ASCII

$webhookScript = @'
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        print("WEBHOOK_PATH=" + self.path, flush=True)
        print(body, flush=True)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        return

HTTPServer(("0.0.0.0", 9099), Handler).handle_request()
'@
Set-Content -Path runtime-data\webhook-smoke.py -Value $webhookScript -Encoding ASCII

$composeConfig = @'
services:
  alert-webhook-smoke:
    image: data-core-api
    volumes:
      - ./runtime-data/webhook-smoke.py:/tmp/webhook-smoke.py:ro
    command: python /tmp/webhook-smoke.py

  alertmanager-smoke:
    image: prom/alertmanager:v0.27.0
    ports:
      - "9094:9093"
    volumes:
      - ./runtime-data/alertmanager-smoke.yml:/etc/alertmanager/alertmanager.yml:ro
    command:
      - "--config.file=/etc/alertmanager/alertmanager.yml"
      - "--storage.path=/alertmanager"
    depends_on:
      - alert-webhook-smoke
'@
Set-Content -Path $ComposeProjectFile -Value $composeConfig -Encoding ASCII

try {
  docker-compose.exe -f docker-compose.yml -f $ComposeProjectFile up -d --force-recreate alert-webhook-smoke alertmanager-smoke | Out-Host

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
    throw "Alertmanager smoke service did not become ready at $readyUrl"
  }

  $now = (Get-Date).ToUniversalTime()
  $payload = @"
[
  {
    "labels": {
      "alertname": "DataCoreWebhookSmoke",
      "service": "data-core",
      "severity": "warning"
    },
    "annotations": {
      "summary": "Data Core webhook smoke test"
    },
    "startsAt": "$($now.ToString("o"))",
    "endsAt": "$($now.AddMinutes(2).ToString("o"))"
  }
]
"@
  Set-Content -Path runtime-data\alertmanager-smoke-alert.json -Value $payload -Encoding ASCII
  curl.exe -sS -X POST "$AlertmanagerUrl/api/v2/alerts" -H "Content-Type: application/json" --data-binary "@runtime-data\alertmanager-smoke-alert.json" | Out-Null
  Start-Sleep -Seconds 5

  $logs = docker-compose.exe -f docker-compose.yml -f $ComposeProjectFile logs --tail=120 alert-webhook-smoke
  $logs | Out-Host

  if (($logs -notmatch "WEBHOOK_PATH=/alerts") -or ($logs -notmatch "DataCoreWebhookSmoke")) {
    throw "Alertmanager smoke alert was not delivered to the webhook receiver."
  }

  Write-Host "Alertmanager smoke webhook delivered successfully."
} finally {
  docker-compose.exe -f docker-compose.yml -f $ComposeProjectFile rm -sf alert-webhook-smoke alertmanager-smoke | Out-Host
}
