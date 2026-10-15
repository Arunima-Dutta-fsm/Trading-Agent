"""Project-relative paths. Nothing here depends on where the repo is cloned."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "configs"
STUDENTS_FILE = CONFIG_DIR / "students.json"
STRATEGY_FILE = CONFIG_DIR / "strategy.json"
CONFIG_HISTORY_DIR = CONFIG_DIR / "history"

DATA_DIR = Path(os.getenv("AABA_DATA_DIR", ROOT / "data"))
INDICATOR_DIR = DATA_DIR / "indicators"
VALIDATION_DIR = DATA_DIR / "validation"
FAKE_BROKER_DIR = DATA_DIR / "fake_broker"
MODEL_RUN_DIR = Path(os.getenv("AABA_MODEL_RUN_DIR", ROOT / "model-run"))
LOG_DIR = Path(os.getenv("AABA_LOG_DIR", ROOT / "logs"))
REPORT_DIR = Path(os.getenv("AABA_REPORT_DIR", ROOT / "reports"))
BACKTEST_DIR = REPORT_DIR / "backtests"
LOCK_FILE = DATA_DIR / "run.lock"
MCP_SERVER_SCRIPT = ROOT / "mcp_server" / "technical_indicators_server.py"


def db_path() -> Path:
    return Path(os.getenv("AABA_DB_PATH", DATA_DIR / "aaba.db"))


def ensure_dirs() -> None:
    for d in (DATA_DIR, INDICATOR_DIR, VALIDATION_DIR, MODEL_RUN_DIR, LOG_DIR,
              REPORT_DIR, BACKTEST_DIR, CONFIG_HISTORY_DIR):
        d.mkdir(parents=True, exist_ok=True)
    db_path().parent.mkdir(parents=True, exist_ok=True)
