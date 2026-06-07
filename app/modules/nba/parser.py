"""
NBA pick text parser.
Supports: Moneyline, Spread, Total (Over/Under), Player Props.

Input format examples:
  "Lakers ML -150"
  "Celtics -3.5 (-110)"
  "Over 224.5 -108"
  "LeBron James Over 25.5 points -115"
  "Warriors +5.5 (+105)"
"""
import re
from dataclasses import dataclass

from app.modules.nba.models import PickType


@dataclass
class ParsedPick:
    pick_type: PickType
    team: str | None
    player: str | None
    line: float | None
    odd: float
    event_description: str | None
    raw_text: str
    parse_status: str = "ok"


_KNOWN_TEAMS = {
    "lakers", "celtics", "warriors", "nets", "bucks", "heat", "bulls", "knicks",
    "76ers", "suns", "clippers", "nuggets", "jazz", "spurs", "mavs", "mavericks",
    "rockets", "thunder", "blazers", "pistons", "pacers", "hawks", "hornets",
    "magic", "wizards", "cavaliers", "cavs", "raptors", "pelicans", "grizzlies",
    "timberwolves", "kings", "trail blazers",
}

_ODD_RE = re.compile(r"([+-]\d{3,4}|\d+\.\d+)")
_LINE_RE = re.compile(r"([+-]?\d+\.?\d*)")
_SPREAD_RE = re.compile(r"^(.+?)\s+([+-]\d+\.?\d*)\s*(\([+-]?\d+\)|\s*[+-]\d{3,4})?$", re.IGNORECASE)  # noqa: E501
_TOTAL_RE = re.compile(r"^(over|under|o|u)\s+(\d+\.?\d*)\s*(\([+-]?\d+\)|\s*[+-]\d{3,4})?$", re.IGNORECASE)  # noqa: E501
_ML_RE = re.compile(r"^(.+?)\s+ml\s*([+-]\d{3,4})$", re.IGNORECASE)
_PLAYER_PROP_RE = re.compile(
    r"^(.+?)\s+(over|under|o|u)\s+(\d+\.?\d*)\s*\w*\s*([+-]\d{3,4}|\d+\.\d+)?$", re.IGNORECASE
)


def _extract_american_odd(text: str) -> float | None:
    m = re.search(r"[+-]\d{3,4}", text)
    if m:
        return float(m.group())
    m = re.search(r"\(([+-]\d{3,4})\)", text)
    if m:
        return float(m.group(1))
    m = re.search(r"\b(\d+\.\d+)\b", text)
    if m:
        return float(m.group(1))
    return None


def _is_likely_player(name: str) -> bool:
    parts = name.strip().split()
    if len(parts) < 2:
        return False
    if any(p.lower() in _KNOWN_TEAMS for p in parts):
        return False
    return all(p[0].isupper() for p in parts if p)


def parse_pick(raw: str, event_description: str | None = None) -> ParsedPick:
    text = raw.strip()
    base = re.sub(r"\(.*?\)", " ", text).strip()

    # Total (Over/Under)
    m = _TOTAL_RE.match(base)
    if m:
        direction = m.group(1).lower()
        line = float(m.group(2))
        odd = _extract_american_odd(text) or -110.0
        team_label = "Over" if direction in ("over", "o") else "Under"
        return ParsedPick(
            pick_type=PickType.total,
            team=f"{team_label} {line}",
            player=None,
            line=line,
            odd=odd,
            event_description=event_description,
            raw_text=raw,
        )

    # Moneyline
    m = _ML_RE.match(base)
    if m:
        return ParsedPick(
            pick_type=PickType.moneyline,
            team=m.group(1).strip(),
            player=None,
            line=None,
            odd=float(m.group(2)),
            event_description=event_description,
            raw_text=raw,
        )

    # Player prop — detect player name (2+ capitalized words) before Over/Under
    m = _PLAYER_PROP_RE.match(base)
    if m and _is_likely_player(m.group(1)):
        line = float(m.group(3))
        direction = m.group(2).lower()
        odd = _extract_american_odd(text) or -110.0
        team_label = "Over" if direction in ("over", "o") else "Under"
        return ParsedPick(
            pick_type=PickType.player_prop,
            team=None,
            player=m.group(1).strip(),
            line=line,
            odd=odd,
            event_description=event_description,
            raw_text=raw,
            parse_status="ok",
        )

    # Spread
    m = _SPREAD_RE.match(base)
    if m:
        odd = _extract_american_odd(text) or -110.0
        try:
            line = float(m.group(2))
        except ValueError:
            line = None
        return ParsedPick(
            pick_type=PickType.spread,
            team=m.group(1).strip(),
            player=None,
            line=line,
            odd=odd,
            event_description=event_description,
            raw_text=raw,
        )

    # Fallback — treat as moneyline if there's an odd
    odd = _extract_american_odd(text)
    if odd is not None:
        return ParsedPick(
            pick_type=PickType.moneyline,
            team=base,
            player=None,
            line=None,
            odd=odd,
            event_description=event_description,
            raw_text=raw,
            parse_status="fallback",
        )

    return ParsedPick(
        pick_type=PickType.moneyline,
        team=None,
        player=None,
        line=None,
        odd=0.0,
        event_description=event_description,
        raw_text=raw,
        parse_status="error",
    )
