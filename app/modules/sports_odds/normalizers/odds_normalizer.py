from app.modules.sports_odds.parsers import NbaOddsParser
from app.normalization.models import NormalizedSportsOdd
from app.normalization.services import BaseNormalizer
from app.raw.models import RawCollection


class SportsOddsNormalizer(BaseNormalizer):
    module = "sports_odds"
    normalizer_name = "generic_odds_normalizer"
    normalizer_version = "1.0.0"
    normalized_model_classes = (NormalizedSportsOdd,)

    def __init__(self, db):
        super().__init__(db)
        self.parser = NbaOddsParser()

    def normalize(self, raw: RawCollection) -> list[dict]:
        payload = raw.raw_content
        if payload is None and isinstance(raw.raw_json, dict):
            payload = raw.raw_json.get("payload")
            if not isinstance(payload, str):
                simple = self._normalize_simple_json(raw, raw.raw_json)
                return [simple] if simple else []
        if not isinstance(payload, str):
            return []
        parsed = self.parser.parse(payload, endpoint=raw.endpoint or "", sportsbook_name=raw.source_name)
        rows: list[dict] = []
        for event in parsed.events:
            for market in event.markets:
                rows.append(
                    {
                        "sportsbook": market.bookmaker or raw.source_name,
                        "sport": event.sport,
                        "league": event.league_name,
                        "event_external_id": event.external_id,
                        "home_team": event.home_team,
                        "away_team": event.away_team,
                        "start_time": event.start_time,
                        "market_type": market.market_type,
                        "selection": market.selection,
                        "handicap": market.handicap,
                        "odd": market.odd,
                        "implied_probability": (1 / market.odd) if market.odd > 0 else None,
                        "collected_at": raw.collected_at,
                    }
                )
        return rows

    @staticmethod
    def _normalize_simple_json(raw: RawCollection, payload: dict) -> dict | None:
        odd = payload.get("odd")
        if odd is None:
            return None
        return {
            "sportsbook": payload.get("bookmaker") or raw.source_name,
            "sport": payload.get("sport") or raw.metadata_json.get("sport") or "unknown",
            "league": payload.get("league") or raw.metadata_json.get("league") or "unknown",
            "event_external_id": payload.get("event_external_id") or payload.get("event_id") or raw.source_id,
            "home_team": payload.get("home_team") or "unknown_home",
            "away_team": payload.get("away_team") or "unknown_away",
            "start_time": payload.get("start_time"),
            "market_type": payload.get("market_type") or payload.get("market") or "unknown",
            "selection": payload.get("selection") or "unknown",
            "handicap": payload.get("handicap"),
            "odd": odd,
            "implied_probability": (1 / float(odd)) if float(odd) > 0 else None,
            "collected_at": raw.collected_at,
        }

    def save_normalized(self, raw: RawCollection, normalized: object | list[object] | None) -> int:
        if not isinstance(normalized, list):
            return 0
        for row in normalized:
            if isinstance(row, dict):
                self.db.add(NormalizedSportsOdd(raw_collection_id=raw.id, **row))
        self.db.flush()
        return len(normalized)
