import base64
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from api.deps import db_session
from app.analytics.models import TradingAnalytics
from app.normalization.models import NormalizedMarketCandle

router = APIRouter(prefix="/api/v1/crypto", tags=["crypto"])


def _encode_cursor(timestamp: datetime, row_id: uuid.UUID) -> str:
    payload = {"t": timestamp.isoformat(), "id": str(row_id)}
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, str] | None:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return datetime.fromisoformat(payload["t"]), str(payload["id"])
    except Exception:
        return None


@router.get("/candles-feed")
def candles_feed(
    db: Session = Depends(db_session),
    source: str | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
    since_hours: int = Query(default=24, ge=1, le=24 * 90),
    limit: int = Query(default=200, ge=1, le=1000),
    cursor: str | None = Query(default=None, description="Opaque cursor from previous response next_cursor field"),
) -> dict[str, Any]:
    since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    query = (
        db.query(NormalizedMarketCandle)
        .filter(NormalizedMarketCandle.timestamp >= since)
        .order_by(NormalizedMarketCandle.timestamp.desc(), NormalizedMarketCandle.id.desc())
    )
    if source:
        query = query.filter(NormalizedMarketCandle.source == source)
    if symbol:
        query = query.filter(NormalizedMarketCandle.symbol == symbol)
    if timeframe:
        query = query.filter(NormalizedMarketCandle.timeframe == timeframe)
    if cursor:
        decoded = _decode_cursor(cursor)
        if decoded:
            cur_ts, cur_id = decoded
            query = query.filter(
                or_(
                    NormalizedMarketCandle.timestamp < cur_ts,
                    and_(NormalizedMarketCandle.timestamp == cur_ts, NormalizedMarketCandle.id < cur_id),
                )
            )

    candles = query.limit(limit).all()
    next_cursor = _encode_cursor(candles[-1].timestamp, candles[-1].id) if len(candles) == limit else None

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "since": since.isoformat(),
        "count": len(candles),
        "next_cursor": next_cursor,
        "items": [_candle_item(candle) for candle in candles],
    }


@router.get("/signals-feed")
def signals_feed(
    db: Session = Depends(db_session),
    source: str | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
    since_hours: int = Query(default=24, ge=1, le=24 * 90),
    limit: int = Query(default=200, ge=1, le=1000),
    cursor: str | None = Query(default=None, description="Opaque cursor from previous response next_cursor field"),
) -> dict[str, Any]:
    since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    query = (
        db.query(TradingAnalytics, NormalizedMarketCandle)
        .join(NormalizedMarketCandle, TradingAnalytics.market_candle_id == NormalizedMarketCandle.id)
        .filter(NormalizedMarketCandle.timestamp >= since)
        .order_by(NormalizedMarketCandle.timestamp.desc(), TradingAnalytics.id.desc())
    )
    if source:
        query = query.filter(NormalizedMarketCandle.source == source)
    if symbol:
        query = query.filter(NormalizedMarketCandle.symbol == symbol)
    if timeframe:
        query = query.filter(NormalizedMarketCandle.timeframe == timeframe)
    if cursor:
        decoded = _decode_cursor(cursor)
        if decoded:
            cur_ts, cur_id = decoded
            query = query.filter(
                or_(
                    NormalizedMarketCandle.timestamp < cur_ts,
                    and_(NormalizedMarketCandle.timestamp == cur_ts, TradingAnalytics.id < cur_id),
                )
            )

    rows = query.limit(limit).all()
    next_cursor = _encode_cursor(rows[-1][1].timestamp, rows[-1][0].id) if len(rows) == limit else None

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "since": since.isoformat(),
        "count": len(rows),
        "next_cursor": next_cursor,
        "items": [_signal_item(analytics, candle) for analytics, candle in rows],
    }


def _candle_item(candle: NormalizedMarketCandle) -> dict[str, Any]:
    return {
        "id": str(candle.id),
        "source": candle.source,
        "symbol": candle.symbol,
        "timeframe": candle.timeframe,
        "timestamp": candle.timestamp.isoformat() if candle.timestamp else None,
        "open": _float(candle.open),
        "high": _float(candle.high),
        "low": _float(candle.low),
        "close": _float(candle.close),
        "volume": _float(candle.volume),
        "normalizer_name": candle.normalizer_name,
        "normalizer_version": candle.normalizer_version,
    }


def _signal_item(analytics: TradingAnalytics, candle: NormalizedMarketCandle) -> dict[str, Any]:
    return {
        "id": str(analytics.id),
        "market_candle_id": str(candle.id),
        "source": candle.source,
        "symbol": candle.symbol,
        "timeframe": candle.timeframe,
        "timestamp": candle.timestamp.isoformat() if candle.timestamp else None,
        "price": _float(candle.close),
        "signal": analytics.signal or "HOLD",
        "confidence": analytics.confidence,
        "regime": analytics.regime,
        "rsi": _float(analytics.rsi),
        "moving_average_fast": _float(analytics.moving_average_fast),
        "moving_average_slow": _float(analytics.moving_average_slow),
        "atr": _float(analytics.atr),
        "adx": _float(analytics.adx),
        "volume_ratio": _float(analytics.volume_ratio),
        "breakout_score": _float(analytics.breakout_score),
        "trend_score": _float(analytics.trend_score),
        "calculated_at": analytics.calculated_at.isoformat() if analytics.calculated_at else None,
    }


def _float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)
