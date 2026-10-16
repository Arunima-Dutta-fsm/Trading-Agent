"""Configuration: .env secrets, students.json and strategy.json, all validated.

Secrets only ever come from environment variables (.env). They are never written
to the database, the reports or the logs (see redact()).
"""
from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator

from . import demo, paths

if os.getenv("AABA_SKIP_DOTENV", "").lower() not in ("1", "true"):   # tests set this so a real .env is ignored
    # on the server the secrets live outside the code folder (deploy/README: /etc/aaba/engine.env)
    load_dotenv(os.getenv("AABA_ENV_FILE") or (paths.ROOT / ".env"), override=False)

RunMode = Literal["analysis", "dry_run", "paper"]
SYMBOL_RE = re.compile(r"^[A-Z][A-Z.]{0,9}$")


# --------------------------------------------------------------------------- strategy
class IndicatorCfg(BaseModel):
    ema_fast: int = Field(20, ge=2, le=100)
    ema_mid: int = Field(50, ge=5, le=150)
    ema_slow: int = Field(200, ge=50, le=300)
    rsi_length: int = Field(14, ge=2, le=50)
    macd_fast: int = Field(12, ge=2, le=50)
    macd_slow: int = Field(26, ge=5, le=100)
    macd_signal: int = Field(9, ge=2, le=50)
    atr_length: int = Field(14, ge=2, le=50)
    bb_length: int = Field(20, ge=5, le=100)
    bb_std: float = Field(2.0, gt=0.5, le=4.0)
    obv_avg_length: int = Field(20, ge=2, le=100)

    @model_validator(mode="after")
    def _order(self):
        if not self.ema_fast < self.ema_mid < self.ema_slow:
            raise ValueError("EMA lengths must be fast < mid < slow")
        if not self.macd_fast < self.macd_slow:
            raise ValueError("MACD fast must be shorter than MACD slow")
        return self


class SatelliteCfg(BaseModel):
    rsi_min: float = Field(50, ge=0, le=100)
    rsi_max: float = Field(70, ge=0, le=100)
    macd_cross_lookback: int = Field(3, ge=1, le=10)
    require_obv_confirm: bool = True
    exit_below_ema_fast: bool = True
    trail_atr_mult: float = Field(3.0, gt=0, le=10)

    @model_validator(mode="after")
    def _band(self):
        if self.rsi_min >= self.rsi_max:
            raise ValueError("satellite rsi_min must be below rsi_max")
        return self


class CoreCfg(BaseModel):
    rsi_oversold: float = Field(35, ge=5, le=60)
    oversold_lookback: int = Field(3, ge=1, le=10)
    rsi_exit: float = Field(65, ge=40, le=95)
    exit_at_upper_band: bool = True


class CompositeCfg(BaseModel):
    weight_trend: float = Field(0.3, ge=0, le=1)
    weight_momentum: float = Field(0.3, ge=0, le=1)
    weight_volume: float = Field(0.2, ge=0, le=1)
    weight_news: float = Field(0.2, ge=0, le=1)
    buy_above: float = Field(0.65, ge=0, le=1)
    sell_below: float = Field(0.40, ge=0, le=1)

    @model_validator(mode="after")
    def _weights(self):
        total = self.weight_trend + self.weight_momentum + self.weight_volume + self.weight_news
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"composite weights must add up to 1.0 (now {total:.3f})")
        if self.sell_below >= self.buy_above:
            raise ValueError("composite sell_below must be lower than buy_above")
        return self


class RiskCfg(BaseModel):
    stop_atr_mult: float = Field(2.0, gt=0, le=10)
    risk_per_trade_pct: float = Field(1.0, gt=0, le=5)
    max_position_pct: float = Field(15.0, gt=0, le=15.0)   # proposal cap: 15% per position
    satellite_allocation: float = Field(10000, gt=0)
    core_allocation: float = Field(7500, gt=0)
    min_order_value: float = Field(200, ge=1)
    max_new_buys_per_run: int = Field(6, ge=0, le=12)
    daily_loss_halt_pct: float = Field(5.0, gt=0, le=50)
    max_price_age_minutes: float = Field(15, gt=0, le=240)
    require_market_open: bool = True


class NewsCfg(BaseModel):
    enabled: bool = True
    lookback_hours: int = Field(48, ge=1, le=240)
    veto_below: float = Field(-0.4, ge=-1, le=0)
    min_articles: int = Field(1, ge=1, le=20)


class EarningsCfg(BaseModel):
    enabled: bool = True
    blackout_days: int = Field(2, ge=0, le=10)
    block_if_unknown: bool = False


class DataCfg(BaseModel):
    feed: Literal["sip", "iex"] = "sip"
    history_days: int = Field(420, ge=320, le=2000)   # enough daily bars for EMA200
    sip_delay_minutes: int = Field(16, ge=15, le=60)
    max_bar_age_sessions: int = Field(0, ge=0, le=3)   # 0 = must include the last completed session


class ApprovalCfg(BaseModel):
    expiry_hours: float = Field(24, gt=0, le=120)
    max_price_move_pct: float = Field(5.0, gt=0, le=30)
    lockout_attempts: int = Field(5, ge=1, le=20)
    lockout_minutes: int = Field(15, ge=1, le=1440)


class ScheduleCfg(BaseModel):
    timezone: str = "America/New_York"
    run_times: list[str] = ["09:45"]
    mode: RunMode = "analysis"
    approved_sell_check_minutes: int = Field(15, ge=5, le=120)

    @field_validator("run_times")
    @classmethod
    def _times(cls, v):
        for t in v:
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
                raise ValueError(f"run time '{t}' must look like HH:MM")
        return v


class AgentCfg(BaseModel):
    use_llm: bool = True
    safety_net_executes_buys: bool = True
    allow_agent_veto: bool = True          # may the LLM turn a BUY into a HOLD? switch off for weak models
    max_iterations: int = Field(15, ge=3, le=60)
    max_rpm: int = Field(20, ge=1, le=600)
    crew_time_budget_seconds: int = Field(900, ge=30, le=7200)  # whole agent phase; then the safety net takes over


class PortfolioCfg(BaseModel):
    """Engine v2 (aaba/portfolio.py): regime-scaled, momentum-ranked, volatility-weighted targets."""
    gross_bull: float = Field(0.95, ge=0, le=1)          # share of capital invested in each regime
    gross_neutral: float = Field(0.60, ge=0, le=1)
    gross_bear: float = Field(0.25, ge=0, le=1)
    breadth_bull: float = Field(0.50, ge=0, le=1)        # share of our stocks above their EMA50
    breadth_bear: float = Field(0.40, ge=0, le=1)
    satellite_share: float = Field(0.40, ge=0, le=1)     # proposal: 4 x $10k satellites / 8 x $7.5k core
    max_positions: int = Field(9, ge=1, le=15)           # per student, best momentum first
    momentum_weights: list[float] = [0.5, 0.3, 0.2]      # 20 / 60 / 120-session returns
    chandelier_atr: float = Field(3.0, gt=0, le=10)      # trailing stop: highest close since entry - k x ATR
    trend_exit_atr: float = Field(0.5, ge=0, le=5)       # exit when close < EMA50 - k x ATR
    emergency_stop_atr: float = Field(3.5, gt=0, le=10)  # intraday: price < anchored entry stop
    rebalance_band: float = Field(0.25, ge=0, le=1)      # buy only if below target by this share of the target
    max_chase_atr: float = Field(0.5, ge=0, le=5)        # no BUY more than this x ATR above VWAP
    news_tilt: float = Field(0.3, ge=0, le=1)            # weight x (1 + tilt x sentiment)
    max_position_risk_pct: float = Field(2.0, gt=0, le=5)  # position risk to its stop, % of capital

    @field_validator("momentum_weights")
    @classmethod
    def _three(cls, v):
        if len(v) != 3 or any(x < 0 for x in v) or abs(sum(v) - 1) > 1e-6:
            raise ValueError("momentum_weights: three non-negative weights adding up to 1")
        return v


class AdaptiveCfg(BaseModel):
    """Engine v3 (aaba/adaptive.py): stock-specific profiles, confirmed exits, deliberate holding.
    The tunable values below were chosen on the TUNING window only (see configs/v3_frozen.json)."""
    gross_bull: float = Field(0.98, ge=0, le=1)
    gross_neutral: float = Field(0.80, ge=0, le=1)
    gross_bear: float = Field(0.40, ge=0, le=1)
    profile_days: int = Field(250, ge=120, le=750)        # trailing sessions used for each stock's profile
    pullback_quantile: float = Field(0.90, gt=0.5, lt=1)  # "normal" pullback depth from the 20-day high, ATR units
    stop_scale: float = Field(1.0, gt=0, le=3)            # stock-specific trailing stop = scale x normal pullback
    stop_min_atr: float = Field(3.0, gt=0, le=10)
    stop_max_atr: float = Field(7.0, gt=0, le=15)
    confirm_days: int = Field(2, ge=1, le=10)             # consecutive closes beyond the stop needed to exit
    loss_cap_atr: float = Field(5.0, gt=0, le=20)         # hard cap: close below entry - k x ATR always exits
    cooldown_days: int = Field(5, ge=0, le=30)            # no re-entry after an exit, unless a new 20-day high
    er_trend: float = Field(0.30, ge=0, le=1)             # efficiency ratio above this: "trend" stock
    revert_autocorr: float = Field(-0.05, ge=-1, le=0)    # lag-1 autocorrelation below this: "mean-reverting"
    max_tilt: float = Field(0.5, ge=0, le=1)              # weight = base allocation x (1 +/- tilt)
    rebalance_band: float = Field(0.25, ge=0, le=1)


class IntradayCfg(BaseModel):
    """Intraday decisions (aaba/intraday.py). Off by default: the daily strategy is unchanged."""
    enabled: bool = False
    engine: Literal["v1", "v2", "v3"] = "v1"             # v1 rules+dips; v2 momentum portfolio; v3 adaptive per stock
    cycle_minutes: int = Field(10, ge=5, le=60)          # re-evaluate every N minutes in the session
    bar_minutes: int = Field(5, ge=1, le=30)             # completed intraday candles of this length
    first_cycle: str = "09:50"                           # New York time
    last_cycle: str = "15:40"                            # no new decisions in the last 20 minutes
    feed: Literal["iex", "sip"] = "iex"                  # free plans: IEX is real-time, SIP is 15 min late
    lookback_days: int = Field(5, ge=2, le=20)           # sessions of intraday history for indicators
    max_bar_age_minutes: float = Field(20, gt=0, le=120) # newest completed candle must be this fresh
    llm_only_when_actionable: bool = True                # call the LLM agent only when a cycle has BUY/SELL
    max_entries_per_position: int = Field(3, ge=1, le=6)
    initial_entry_fraction: float = Field(0.5, gt=0, le=1)  # first entry uses at most this share of the allocation
    min_minutes_between_entries: int = Field(30, ge=5, le=390)
    min_add_drop_atr: float = Field(0.25, gt=0, le=3)    # each add at least this x daily ATR below the last entry
    max_position_risk_pct: float = Field(1.5, gt=0, le=3)  # risk to the anchored stop, % of starting capital
    max_buys_per_student_per_day: int = Field(10, ge=0, le=60)
    min_reward_risk: float = Field(1.5, gt=0, le=10)
    min_relative_volume: float = Field(0.8, ge=0, le=5)  # volume of the confirming candle vs its 20-candle average
    market_weak_drop_pct: float = Field(1.0, gt=0, le=10)  # SPY down this much and below VWAP: no new entries
    profit_take_rsi: float = Field(80, ge=50, le=100)
    cost_bps: float = Field(5, ge=0, le=100)             # slippage + spread estimate per side, in basis points

    @field_validator("first_cycle", "last_cycle")
    @classmethod
    def _hhmm(cls, v):
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError(f"'{v}' must look like HH:MM")
        return v


class ReportCfg(BaseModel):
    timezone: str = "Asia/Kolkata"


class StrategyConfig(BaseModel):
    signal_mode: Literal["rules", "composite"] = "rules"
    indicators: IndicatorCfg = IndicatorCfg()
    satellite: SatelliteCfg = SatelliteCfg()
    core: CoreCfg = CoreCfg()
    composite: CompositeCfg = CompositeCfg()
    risk: RiskCfg = RiskCfg()
    news: NewsCfg = NewsCfg()
    earnings: EarningsCfg = EarningsCfg()
    data: DataCfg = DataCfg()
    approvals: ApprovalCfg = ApprovalCfg()
    schedule: ScheduleCfg = ScheduleCfg()
    agent: AgentCfg = AgentCfg()
    intraday: IntradayCfg = IntradayCfg()
    portfolio: PortfolioCfg = PortfolioCfg()
    adaptive: AdaptiveCfg = AdaptiveCfg()
    reports: ReportCfg = ReportCfg()


# --------------------------------------------------------------------------- students
class Replacement(BaseModel):
    old: str
    new: str
    date: str
    reason: str


class StudentConfig(BaseModel):
    student_id: str
    name: str
    roll_no: str
    enabled: bool = True
    env_prefix: str
    starting_capital: float = 100000
    satellites: list[str]
    core: list[str]
    replacements: list[Replacement] = []

    @field_validator("satellites", "core")
    @classmethod
    def _symbols(cls, v):
        out = [s.strip().upper() for s in v]
        for s in out:
            if not SYMBOL_RE.match(s):
                raise ValueError(f"'{s}' is not a valid US stock symbol")
        return out

    @model_validator(mode="after")
    def _universe(self):
        syms = self.satellites + self.core
        if len(set(syms)) != len(syms):
            raise ValueError(f"{self.student_id}: a symbol is listed twice")
        if not 10 <= len(syms) <= 15:
            raise ValueError(f"{self.student_id}: the brief asks for 10-15 stocks, found {len(syms)}")
        return self

    @property
    def symbols(self) -> list[str]:
        return self.satellites + self.core

    @property
    def label(self) -> str:
        return f"{self.name} ({self.roll_no})"

    def category(self, symbol: str) -> str:
        symbol = symbol.upper()
        if symbol in self.satellites:
            return "satellite"
        if symbol in self.core:
            return "core"
        raise KeyError(f"{symbol} is not in {self.student_id}'s portfolio")

    # credentials are read from the environment on demand and never stored
    def alpaca_keys(self) -> tuple[str, str]:
        if demo.is_read_only():
            return "", ""
        return (os.getenv(f"{self.env_prefix}_ALPACA_API_KEY", "").strip(),
                os.getenv(f"{self.env_prefix}_ALPACA_SECRET_KEY", "").strip())

    def expected_account_number(self) -> str:
        return os.getenv(f"{self.env_prefix}_ALPACA_ACCOUNT_NUMBER", "").strip()

    def approval_pin(self) -> str:
        if demo.is_read_only():
            return ""
        return os.getenv(f"{self.env_prefix}_APPROVAL_PIN", "").strip()


class GroupConfig(BaseModel):
    group_name: str = "AABA Group"
    reserves: dict[str, list[str]] = {"satellite": [], "core": []}
    students: list[StudentConfig]

    @model_validator(mode="after")
    def _unique(self):
        ids = [s.student_id for s in self.students]
        if len(set(ids)) != len(ids):
            raise ValueError("student_id values must be unique")
        all_syms = [x for s in self.students for x in s.symbols]
        dupes = sorted({x for x in all_syms if all_syms.count(x) > 1})
        if dupes:
            raise ValueError(f"symbols shared between students: {dupes}")
        prefixes = [s.env_prefix for s in self.students]
        if len(set(prefixes)) != len(prefixes):
            raise ValueError("each student needs its own env_prefix")
        # every logged replacement must match the current list, and satellite swaps must use a reserve
        reserves = {x.upper() for x in self.reserves.get("satellite", [])}
        for st in self.students:
            for r in st.replacements:
                old, new = r.old.upper(), r.new.upper()
                if old in st.symbols:
                    raise ValueError(f"{st.student_id}: replaced stock {old} is still in the list")
                if new not in st.symbols:
                    raise ValueError(f"{st.student_id}: replacement {new} is not in the list")
                if new in st.satellites and new not in reserves:
                    raise ValueError(f"{st.student_id}: satellite replacement {new} is not an approved reserve "
                                     f"({', '.join(sorted(reserves))})")
                if not r.reason.strip():
                    raise ValueError(f"{st.student_id}: replacement {old}->{new} needs a reason")
        return self

    def original_symbols(self, student_id: str) -> tuple[list[str], list[str]]:
        """The stock lists as first proposed, by undoing the logged replacements (newest first)."""
        st = self.get(student_id)
        sats, core = list(st.satellites), list(st.core)
        for r in reversed(st.replacements):
            for lst in (sats, core):
                if r.new.upper() in lst:
                    lst[lst.index(r.new.upper())] = r.old.upper()
        return sats, core

    def get(self, student_id: str) -> StudentConfig:
        for s in self.students:
            if s.student_id == student_id:
                return s
        raise KeyError(f"unknown student_id '{student_id}'")

    @property
    def enabled(self) -> list[StudentConfig]:
        return [s for s in self.students if s.enabled]

    @property
    def all_symbols(self) -> list[str]:
        return [x for s in self.students for x in s.symbols]


def _read_json(path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_students() -> GroupConfig:
    raw = {k: v for k, v in _read_json(paths.STUDENTS_FILE).items() if not k.startswith("_")}
    if demo.is_demo():
        # the public demo never shows real names or roll numbers
        raw["group_name"] = "AABA Group (demo)"
        for i, s in enumerate(raw["students"], 1):
            s["name"], s["roll_no"] = f"Student {i}", "demo"
    return GroupConfig(**raw)


def load_strategy() -> StrategyConfig:
    return StrategyConfig(**_read_json(paths.STRATEGY_FILE))


def save_strategy(new: dict, changed_by: str = "dashboard") -> StrategyConfig:
    """Validate first, keep a dated backup of the old file, then save."""
    demo.guard("changing the strategy configuration")
    cfg = StrategyConfig(**new)
    paths.CONFIG_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(paths.STRATEGY_FILE, paths.CONFIG_HISTORY_DIR / f"strategy-{stamp}-{changed_by}.json")
    with open(paths.STRATEGY_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg.model_dump(), f, indent=2)
    return cfg


def set_student_enabled(student_id: str, enabled: bool) -> GroupConfig:
    demo.guard("enabling or disabling a student module")
    raw = _read_json(paths.STUDENTS_FILE)
    for s in raw["students"]:
        if s["student_id"] == student_id:
            s["enabled"] = bool(enabled)
    cfg = GroupConfig(**{k: v for k, v in raw.items() if not k.startswith("_")})
    paths.CONFIG_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(paths.STUDENTS_FILE, paths.CONFIG_HISTORY_DIR /
                 f"students-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
    with open(paths.STUDENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2)
    return cfg


# --------------------------------------------------------------------------- environment
def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def paper_orders_enabled() -> bool:
    """Second switch for real paper orders. Both this and --mode paper are needed.
    In demo mode it is off whatever the environment says (only the sample-data build
    fills simulated orders, on the fake broker)."""
    if demo.is_demo():
        return demo.seeding_active()
    if demo.is_read_only():
        return False
    return env_bool("ENABLE_PAPER_ORDERS", False)


def data_provider() -> str:
    """'alpaca' for real use. 'synthetic' only for tests and offline demos (clearly labelled)."""
    if demo.is_demo():
        return "synthetic"
    return os.getenv("AABA_DATA_PROVIDER", "alpaca").strip().lower()


def broker_backend() -> str:
    """'alpaca' (paper endpoint only) or 'fake' for tests and offline demos."""
    if demo.is_demo():
        return "fake"
    return os.getenv("AABA_BROKER", "alpaca").strip().lower()


def is_simulated() -> bool:
    return data_provider() == "synthetic" or broker_backend() == "fake"


# --------------------------------------------------------------------------- secrets
# only names that really hold secrets: ..._API_KEY, ..._SECRET_KEY, ..._SECRET, ..._TOKEN, ..._PIN, ...PASSWORD
_SECRET_NAME = re.compile(r"(API_KEY|SECRET_KEY|_SECRET|_TOKEN|_PIN|PASSWORD)$", re.I)
_NOT_SECRET = {"true", "false", "none", "null", "ollama", "dummy"}


def secret_values() -> list[str]:
    vals = []
    for k, v in os.environ.items():
        v = (v or "").strip()
        if _SECRET_NAME.search(k) and len(v) >= 6 and v.lower() not in _NOT_SECRET:
            vals.append(v)
    return sorted(set(vals), key=len, reverse=True)


def redact(text: str) -> str:
    """Remove any secret value (env vars named *_API_KEY, *_SECRET_KEY, *_TOKEN, *_PIN ...) from text."""
    if not text:
        return text
    out = str(text)
    for v in secret_values():
        if v.isdigit() or len(v) < 12:
            # short or numeric secrets (PINs) only count as a whole token, so a PIN like 481516
            # never corrupts a number such as 0.4815162 in a report
            out = re.sub(rf"(?<![0-9A-Za-z]){re.escape(v)}(?![0-9A-Za-z])", "[REDACTED]", out)
        else:
            out = out.replace(v, "[REDACTED]")
    return out
