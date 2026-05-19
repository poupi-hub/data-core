# Telegram Integration — Watchdog Alerts & Heartbeat

## 1. Configuração do Bot

### 1.1 Criar bot via @BotFather

1. Abrir Telegram → buscar `@BotFather`
2. `/newbot` → escolher nome e username (ex: `PoupiWatchdogBot`)
3. Copiar o **bot token** gerado (formato: `123456789:ABCdef...`)

### 1.2 Obter Chat ID

```bash
# 1. Adicionar o bot ao grupo/canal desejado
# 2. Enviar qualquer mensagem no grupo
# 3. Consultar updates do bot
curl "https://api.telegram.org/bot<TOKEN>/getUpdates" | jq '.result[].message.chat.id'
```

O `chat_id` para grupos é negativo (ex: `-1001234567890`).

---

## 2. Configuração no data-core

Adicionar ao `.env`:

```env
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjkl...
TELEGRAM_CHAT_ID=-1001234567890
```

Testar via API:
```bash
curl -X POST -H "X-API-Key: $API_KEY" \
  http://localhost:8000/api/v1/watchdog/heartbeat/send
```

---

## 3. Integração poupi-baby → data-core

O `TelegramPublicationChecker` depende de callbacks do poupi-baby para saber quando mensagens
foram enviadas ao Telegram. Sem esse callback, o checker reporta `ok` com nota informativa.

### 3.1 Endpoint de callback

```
POST /api/v1/watchdog/report/telegram-published
X-API-Key: <data-core-api-key>
Content-Type: application/json
```

Payload:
```json
{
  "group_id": "poupi-oportunidades",
  "product_id": "uuid-do-produto",
  "offer_id": "uuid-da-oferta",
  "marketplace": "drogasil",
  "price": 29.90,
  "deal_score": 82.5,
  "status": "sent",
  "fail_reason": null
}
```

Valores de `status`:
- `"sent"` — mensagem enviada com sucesso
- `"failed"` — erro no envio (incluir `fail_reason`)
- `"rate_limited"` — Telegram retornou 429

### 3.2 Integração no poupi-baby (TypeScript)

Adicionar no `TelegramGroupProcessor` (ou equivalente) após cada tentativa de envio:

```typescript
import axios from 'axios';

async function reportToWatchdog(
  productId: string,
  marketplace: string,
  price: number,
  dealScore: number,
  status: 'sent' | 'failed' | 'rate_limited',
  failReason?: string
): Promise<void> {
  const dataCoreUrl = process.env.DATA_CORE_URL;
  const apiKey = process.env.DATA_CORE_API_KEY;
  if (!dataCoreUrl || !apiKey) return; // opcional — não quebrar se não configurado

  try {
    await axios.post(
      `${dataCoreUrl}/api/v1/watchdog/report/telegram-published`,
      {
        group_id: 'poupi-oportunidades',
        product_id: productId,
        marketplace,
        price,
        deal_score: dealScore,
        status,
        fail_reason: failReason ?? null,
      },
      {
        headers: { 'X-API-Key': apiKey },
        timeout: 5000,
      }
    );
  } catch {
    // silencioso — não bloquear o fluxo principal
  }
}
```

Variáveis de ambiente no poupi-baby:
```env
DATA_CORE_URL=http://data-core:8000
DATA_CORE_API_KEY=<api-key>
```

---

## 4. Formato das mensagens

### Alerta crítico imediato
```
🔴 Coleta parada: drogasil

Nenhuma coleta de 'drogasil' nas últimas 3h. Última coleta: há 5.2h.

code: collection_stale
2026-05-18 14:30 UTC
```

### Heartbeat a cada 6h (quando ok)
```
✅ Poupi saudável
2026-05-18 14:30 UTC

📦 Coleta: OK
  • Fontes ativas: 3
  • Última coleta: há 45 min

🔄 Normalização: OK
  • Normalizados 24h: 142
  • Pendentes: 0
  • Último normalizado: há 1.2h

📣 Telegram: OK
  • Enviados 24h: 7 | Falhas: 0
  • Última publicação: há 2.0h

🔍 Qualidade: OK
  • Fontes monitoradas: 3
  • Qualidade média: 87/100
  • Anti-bot 1h: 0
  • Drift aberto: 0

🕐 Próximo heartbeat: em ~6h
```

### Heartbeat quando há alertas
```
⚠️ Poupi — ATENÇÃO
2026-05-18 14:30 UTC

📦 Coleta: ATENÇÃO
  • Fontes ativas: 2
  • Última coleta: há 20 min
  • Sem coleta: paguemenos

...

⚠️ Alertas ativos:
  ⚠️ Coleta parada: paguemenos
```

---

## 5. Testando alertas manualmente

```bash
# Forçar run do watchdog (sem Telegram)
python -c "
from database.session import SessionLocal
from app.watchdog.service import WatchdogService
db = SessionLocal()
svc = WatchdogService(db)
run = svc.run()
print('status:', run.overall_status)
print('alerts:', run.alert_codes)
db.close()
"

# Forçar heartbeat (envia ao Telegram se configurado)
python -c "
from database.session import SessionLocal
from app.watchdog.service import WatchdogService
db = SessionLocal()
svc = WatchdogService(db)
sent = svc.heartbeat()
print('sent:', sent)
db.close()
"
```
