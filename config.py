"""All input/output paths for the project. Nothing else hardcodes a path."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

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


def out_paths(out_dir: str | Path) -> tuple[Path, Path, Path]:
    """The three output files under an alternative out directory."""
    d = Path(out_dir)
    return d / FILLS_CSV.name, d / FIRM_TRADES_CSV.name, d / SUMMARY_TXT.name
