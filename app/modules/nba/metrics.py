from prometheus_client import Counter, Gauge

nba_picks_total = Counter(
    "nba_picks_total",
    "Total NBA picks captured",
    ["source", "pick_type"],
)

nba_parse_errors_total = Counter(
    "nba_parse_errors_total",
    "Total NBA pick parse errors",
    ["source"],
)

nba_bets_settled_total = Counter(
    "nba_bets_settled_total",
    "Total NBA paper bets settled",
    ["result"],
)

nba_source_roi = Gauge(
    "nba_source_roi",
    "ROI per NBA source (percent)",
    ["source"],
)

nba_source_win_rate = Gauge(
    "nba_source_win_rate",
    "Win rate per NBA source (percent)",
    ["source"],
)

nba_source_pnl = Gauge(
    "nba_source_pnl",
    "Cumulative PnL per NBA source (units)",
    ["source"],
)

nba_global_roi = Gauge("nba_global_roi", "Global NBA ROI (percent)")
nba_global_pnl = Gauge("nba_global_pnl", "Global NBA PnL (units)")
nba_global_win_rate = Gauge("nba_global_win_rate", "Global NBA win rate (percent)")
