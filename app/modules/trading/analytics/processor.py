from dataclasses import dataclass
from typing import Any

import pandas as pd
from app.analytics.models import TradingAnalytics
from app.analytics.services import BaseAnalyticsProcessor
from app.normalization.models import NormalizedMarketCandle
from domains.crypto_coin.indicators.technical import compute_indicators
from domains.crypto_coin.strategies.trend_following.strategy import get_signal


class TradingAnalyticsProcessor(BaseAnalyticsProcessor):
    module = "trading"
    analytics_processor_version = "1.1.0"

    def load_normalized(self, *, limit: int = 100) -> list[NormalizedMarketCandle]:
        return (
            self.db.query(NormalizedMarketCandle)
            .filter(NormalizedMarketCandle.analytics_status == "pending")
            .order_by(NormalizedMarketCandle.timestamp)
            .limit(limit)
            .all()
        )

    def calculate(self, normalized: NormalizedMarketCandle) -> dict:
        candles = (
            self.db.query(NormalizedMarketCandle)
            .filter(
                NormalizedMarketCandle.source == normalized.source,
                NormalizedMarketCandle.symbol == normalized.symbol,
                NormalizedMarketCandle.timeframe == normalized.timeframe,
                NormalizedMarketCandle.timestamp <= normalized.timestamp,
            )
            .order_by(NormalizedMarketCandle.timestamp)
            .limit(250)
            .all()
        )
        df = _candles_to_dataframe(candles)
        cfg = _TradingIndicatorConfig(timeframe=normalized.timeframe)
        indicators = compute_indicators(df, cfg) if not df.empty else None
        signal = get_signal(indicators, in_position=False, buy_price=None, cfg=cfg) if indicators else None

        return {
            "market_candle_id": normalized.id,
            "symbol": normalized.symbol,
            "timeframe": normalized.timeframe,
            "rsi": _value(getattr(indicators, "rsi", None)),
            "moving_average_fast": _value(getattr(indicators, "ma_fast", None)),
            "moving_average_slow": _value(getattr(indicators, "ma_slow", None)),
            "atr": _value(getattr(indicators, "atr", None)),
            "adx": _value(getattr(indicators, "adx", None)),
            "volume_ratio": _value(getattr(indicators, "volume_ratio", None)),
            "breakout_score": _value(getattr(indicators, "breakout_score", None)),
            "trend_score": _trend_score(indicators),
            "signal": signal.name if signal else "HOLD",
            "confidence": getattr(indicators, "confidence", 0) if indicators else 0,
            "regime": getattr(getattr(indicators, "regime", None), "name", None),
        }

    def save_analytics(self, normalized: NormalizedMarketCandle, analytics: object | None) -> int:
        if not isinstance(analytics, dict):
            return 0
        self.db.add(
            TradingAnalytics(
                source_normalizer_name=normalized.normalizer_name,
                source_normalizer_version=normalized.normalizer_version,
                **analytics,
            )
        )
        self.db.flush()
        return 1


@dataclass(frozen=True)
class _TradingIndicatorConfig:
    timeframe: str = "15m"
    ma_fast: int = 9
    ma_slow: int = 21
    rsi_period: int = 14
    rsi_oversold: float = 35.0
    rsi_overbought: float = 70.0
    bb_period: int = 20
    bb_std: float = 2.0
    atr_period: int = 14
    stop_loss_pct: float = 2.0
    take_profit_pct: float = 4.0


def _candles_to_dataframe(candles: list[NormalizedMarketCandle]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for candle in candles:
        rows.append(
            {
                "timestamp": candle.timestamp,
                "open": float(candle.open) if candle.open is not None else None,
                "high": float(candle.high) if candle.high is not None else None,
                "low": float(candle.low) if candle.low is not None else None,
                "close": float(candle.close) if candle.close is not None else None,
                "volume": float(candle.volume) if candle.volume is not None else 0.0,
            }
        )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.dropna(subset=["open", "high", "low", "close"]).set_index("timestamp")


def _value(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _trend_score(indicators: object | None) -> float | None:
    if indicators is None:
        return None
    confidence = _value(getattr(indicators, "confidence", None))
    if confidence is None:
        return None
    regime_name = getattr(getattr(indicators, "regime", None), "name", "")
    direction = -1.0 if regime_name == "TRENDING_DOWN" else 1.0
    return round(direction * confidence / 100.0, 4)
