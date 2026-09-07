"""Project configuration: all input/output paths and the risk/strategy
parameters. Nothing else hardcodes a path or a risk number."""
from __future__ import annotations

from datetime import datetime, time
from pathlib import Path

from internalizer.strategy import Config as StrategyConfig

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
OUT_DIR = BASE_DIR / "out"

# inputs
QUOTES_CSV = DATA_DIR / "aapl_quotes_20260817.csv"
ORDERS_CSV = DATA_DIR / "client_orders_20260817.csv"

# outputs
FILLS_CSV = OUT_DIR / "fills.csv"
FIRM_TRADES_CSV = OUT_DIR / "firm_trades.csv"
SUMMARY_TXT = OUT_DIR / "summary.txt"

# dashboard
DASHBOARD_TEMPLATE = BASE_DIR / "dashboard_template.html"
DASHBOARD_HTML = OUT_DIR / "dashboard.html"

# session
SESSION_OPEN = datetime(2026, 8, 17, 9, 30)

# risk / strategy parameters (principal book limits, pricing threshold, close-down)
STRATEGY = StrategyConfig(
    min_internalize_spread=2,        # cents of quoted spread needed to take principal risk
    hard_position_limit=10_000,      # shares; principal fills are sized so this never breaches
    soft_position_limit=6_000,       # hedge back inside this band when breached
    no_new_risk_after=time(15, 55),  # reduce-only from here to the close
    bleed_trigger=4_000,             # v0.2: proactive bleed fires above this, reduces to it
    cheap_spread_max=1,              # trigger A: spread <= 1c (cheapest hedge windows)
    age_limit_secs=600,              # trigger B: |pos| aged 10 min above trigger
)


def out_paths(out_dir: str | Path) -> tuple[Path, Path, Path]:
    """The three output files under an alternative out directory."""
    d = Path(out_dir)
    return d / FILLS_CSV.name, d / FIRM_TRADES_CSV.name, d / SUMMARY_TXT.name
