from prometheus_client import Counter, Gauge

nba_q_games_collected_total = Counter(
    "nba_q_games_collected_total",
    "Total NBA games collected",
    ["season"],
)

nba_q_signals_total = Counter(
    "nba_q_signals_total",
    "Total NBA quant signals generated",
    ["setup"],
)

nba_q_bets_settled_total = Counter(
    "nba_q_bets_settled_total",
    "Total NBA quant bets settled",
    ["setup", "result"],
)

nba_q_setup_roi = Gauge(
    "nba_q_setup_roi",
    "ROI per setup (%)",
    ["setup"],
)

nba_q_setup_win_rate = Gauge(
    "nba_q_setup_win_rate",
    "Win rate per setup (%)",
    ["setup"],
)

nba_q_setup_classification = Gauge(
    "nba_q_setup_classification",
    "Edge classification (1=PROFITABLE, 0=NEUTRAL, -1=LOSING)",
    ["setup"],
)

nba_q_global_roi = Gauge("nba_q_global_roi", "Global quant ROI (%)")
nba_q_global_pnl = Gauge("nba_q_global_pnl", "Global quant PnL (units)")
nba_q_total_games = Gauge("nba_q_total_games", "Total NBA games in DB")
nba_q_total_signals = Gauge("nba_q_total_signals", "Total quant signals")
