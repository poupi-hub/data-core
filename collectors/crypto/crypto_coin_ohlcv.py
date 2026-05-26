import dataclasses
import logging
import os
from typing import Any

from collectors.base import BaseCollector, CollectedItem, CollectorMetadata
from database.models import CollectorDomain
from domains.crypto_coin.config.settings import load_config
from domains.crypto_coin.core.execution.exchange_connector import ExchangeConnector

logger = logging.getLogger(__name__)

DEFAULT_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "ADA/USDT", "DOGE/USDT", "XRP/USDT"]
DEFAULT_TIMEFRAMES = ["15m", "1h"]


class CryptoCoinOHLCVCollector(BaseCollector):
    metadata = CollectorMetadata(
        name="crypto.crypto_coin_ohlcv",
        domain=CollectorDomain.crypto,
        source="crypto_coin_exchange",
        description="Collects OHLCV candles through the migrated crypto-coin exchange connector.",
        default_interval_minutes=15,
        raw_schema_name="marketCandle",
        raw_schema_version="1.0.0",
    )

    async def collect(self) -> list[CollectedItem]:
        base_cfg = load_config(self.config.get("env_file", ".env"))
        limit = int(self.config.get("limit", 50))

        # Support SYMBOLS (comma-separated) for multi-pair collection.
        raw_symbols = os.getenv("SYMBOLS", "").strip()
        symbols = [s.strip() for s in raw_symbols.split(",") if s.strip()] if raw_symbols else DEFAULT_SYMBOLS

        # Support TIMEFRAMES (comma-separated) for multi-timeframe collection.
        raw_timeframes = os.getenv("TIMEFRAMES", "").strip()
        timeframes = (
            [t.strip() for t in raw_timeframes.split(",") if t.strip()]
            if raw_timeframes
            else DEFAULT_TIMEFRAMES
        )

        items: list[CollectedItem] = []
        for symbol in symbols:
            for timeframe in timeframes:
                cfg = dataclasses.replace(base_cfg, symbol=symbol, timeframe=timeframe)
                connector = ExchangeConnector(cfg, logger)
                try:
                    await connector.connect()
                    df = await connector.fetch_ohlcv(limit=limit)
                except Exception as exc:
                    logger.error(
                        "Failed to fetch OHLCV",
                        extra={"symbol": symbol, "timeframe": timeframe, "error": str(exc)},
                    )
                    continue
                finally:
                    await connector.close()

                if df is None or df.empty:
                    logger.warning("Empty OHLCV response", extra={"symbol": symbol, "timeframe": timeframe})
                    continue

                for timestamp, row in df.tail(limit).iterrows():
                    ts = timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp)
                    payload: dict[str, Any] = {
                        "symbol": cfg.symbol,
                        "exchange": cfg.exchange,
                        "timeframe": cfg.timeframe,
                        "timestamp": ts,
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "volume": float(row["volume"]),
                    }
                    items.append(
                        CollectedItem(
                            external_id=f"{cfg.exchange}:{cfg.symbol}:{cfg.timeframe}:{ts}",
                            source_url=None,
                            payload=payload,
                            metadata={"domain_module": "domains.crypto_coin"},
                        )
                    )
                logger.info(
                    "Collected OHLCV candles",
                    extra={"symbol": symbol, "timeframe": timeframe, "candles": len(df.tail(limit))},
                )

        return items
