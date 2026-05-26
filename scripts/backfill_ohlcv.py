"""
Backfill histórico de OHLCV no data-core.

Baixa dados do Binance via ccxt e insere diretamente em:
  raw_collections (processing_status=normalized)
  normalized_market_candles (analytics_status=pending)

Depois dispara o analytics pipeline via API.

Usage (dentro do container api do data-core):
    python scripts/backfill_ohlcv.py --days 90 --timeframes 1h
    python scripts/backfill_ohlcv.py --days 90 --timeframes 1h,15m
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import urllib.request
from datetime import datetime, timezone, timedelta
UTC = timezone.utc
from uuid import uuid4

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill")

SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "ADA/USDT"]
SOURCE_NAME = "crypto_coin_exchange"
MODULE = "crypto"
RAW_SCHEMA_NAME = "marketCandle"
EXCHANGE_NAME = "binance"


def _checksum(data: dict) -> str:
    return hashlib.md5(json.dumps(data, sort_keys=True).encode()).hexdigest()


async def fetch_ohlcv_paginated(exchange, symbol: str, timeframe: str, since_ms: int) -> list:
    all_bars = []
    cursor = since_ms
    while True:
        bars = await exchange.fetch_ohlcv(symbol, timeframe, since=cursor, limit=1000)
        if not bars:
            break
        all_bars.extend(bars)
        if len(bars) < 1000:
            break
        cursor = bars[-1][0] + 1
        await asyncio.sleep(0.25)
    return all_bars


def insert_bars(db, bars: list, symbol: str, timeframe: str) -> tuple[int, int]:
    from sqlalchemy import text
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    raw_inserted = 0
    candle_inserted = 0

    for bar in bars:
        ts_ms, open_, high, low, close, volume = bar
        ts = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
        ts_iso = ts.isoformat()

        raw_json = {
            "symbol": symbol,
            "exchange": EXCHANGE_NAME,
            "timeframe": timeframe,
            "timestamp": ts_iso,
            "open": float(open_),
            "high": float(high),
            "low": float(low),
            "close": float(close),
            "volume": float(volume),
        }
        external_id = f"{EXCHANGE_NAME}:{symbol}:{timeframe}:{ts_iso}"
        checksum = _checksum(raw_json)

        # Insert raw_collection — conflict on (module, source_name, checksum)
        raw_id = str(uuid4())
        raw_stmt = text("""
            INSERT INTO raw_collections (
                id, module, source_name, source_type, collector_name, collector_version,
                raw_schema_name, raw_schema_version, raw_json, checksum,
                processing_status, source_id, metadata_json, collection_metadata_json,
                collected_at, created_at
            ) VALUES (
                :id, :module, :source_name, 'api', 'crypto.crypto_coin_ohlcv', '1.0.0',
                :schema_name, '1.0.0', :raw_json, :checksum,
                'normalized', :source_id, '{}', '{}',
                :ts, :ts
            )
            ON CONFLICT ON CONSTRAINT uq_raw_collection_identity DO NOTHING
            RETURNING id
        """)
        result = db.execute(raw_stmt, {
            "id": raw_id,
            "module": MODULE,
            "source_name": SOURCE_NAME,
            "schema_name": RAW_SCHEMA_NAME,
            "raw_json": json.dumps(raw_json),
            "checksum": checksum,
            "source_id": external_id,
            "ts": ts,
        })
        row = result.fetchone()
        if row:
            raw_id = str(row[0])
            raw_inserted += 1
        else:
            # Already exists — get the existing id
            existing = db.execute(
                text("SELECT id FROM raw_collections WHERE module=:m AND source_name=:s AND checksum=:c"),
                {"m": MODULE, "s": SOURCE_NAME, "c": checksum}
            ).fetchone()
            if not existing:
                continue
            raw_id = str(existing[0])

        # Insert normalized_market_candle
        candle_stmt = text("""
            INSERT INTO normalized_market_candles (
                id, raw_collection_id, source, symbol, timeframe,
                open, high, low, close, volume, timestamp,
                analytics_status, normalizer_name, normalizer_version,
                normalized_at, normalization_metadata_json
            ) VALUES (
                :id, :raw_id, :source, :symbol, :timeframe,
                :open, :high, :low, :close, :volume, :ts,
                'pending', 'trading_candle_normalizer', '1.0.0',
                NOW(), '{}'
            )
            ON CONFLICT ON CONSTRAINT uq_norm_market_candle_identity DO NOTHING
        """)
        result2 = db.execute(candle_stmt, {
            "id": str(uuid4()),
            "raw_id": raw_id,
            "source": SOURCE_NAME,
            "symbol": symbol,
            "timeframe": timeframe,
            "open": float(open_), "high": float(high),
            "low": float(low), "close": float(close),
            "volume": float(volume),
            "ts": ts,
        })
        candle_inserted += result2.rowcount

    db.commit()
    return raw_inserted, candle_inserted


def trigger_analytics(pending_count: int) -> dict:
    base_url = os.getenv("DATA_CORE_URL", "http://localhost:8000")
    api_key = os.getenv("DATA_CORE_API_KEY", "")
    limit = min(pending_count + 50, 10000)
    url = f"{base_url}/api/v1/operations/pipeline/run?skip_normalize=true&limit={limit}"
    req = urllib.request.Request(url, method="POST")
    if api_key:
        req.add_header("X-API-Key", api_key)
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read())
    except Exception as e:
        logger.error(f"Analytics API call failed: {e}")
        return {}


async def main(days: int, timeframes: list[str]) -> None:
    import ccxt.async_support as ccxt
    from database.session import SessionLocal

    since_ms = int((datetime.now(UTC) - timedelta(days=days)).timestamp() * 1000)
    logger.info(f"Backfill: {days} days back | timeframes={timeframes} | symbols={SYMBOLS}")

    exchange = ccxt.binance({"enableRateLimit": True})
    db = SessionLocal()

    total_raw = 0
    total_candles = 0

    try:
        for symbol in SYMBOLS:
            for timeframe in timeframes:
                logger.info(f"→ {symbol} {timeframe}: downloading from Binance...")
                try:
                    bars = await fetch_ohlcv_paginated(exchange, symbol, timeframe, since_ms)
                    logger.info(f"  downloaded {len(bars)} bars")
                    raw_n, candle_n = insert_bars(db, bars, symbol, timeframe)
                    total_raw += raw_n
                    total_candles += candle_n
                    logger.info(f"  inserted: {raw_n} raw, {candle_n} candles (new)")
                except Exception as e:
                    db.rollback()
                    logger.error(f"  FAILED {symbol} {timeframe}: {e}")

    finally:
        db.close()
        await exchange.close()

    logger.info(f"\nSummary: {total_raw} raw records, {total_candles} candles inserted")

    if total_candles > 0:
        logger.info(f"Triggering analytics pipeline for {total_candles} pending candles...")
        result = trigger_analytics(total_candles)
        logger.info(f"Analytics result: {result}")
    else:
        logger.info("No new candles — nothing to process.")

    logger.info("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--timeframes", type=str, default="1h")
    args = parser.parse_args()
    asyncio.run(main(args.days, [t.strip() for t in args.timeframes.split(",")]))
