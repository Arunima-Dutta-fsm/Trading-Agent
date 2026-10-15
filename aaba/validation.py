"""Pre-trading symbol checks required by the portfolio proposal (Sections 6-8).

For each student, using that student's own paper account:
- the stock exists on Alpaca, is active and tradable (and whether it is fractionable),
- there is enough daily history (at least 250 bars, for EMA200 and a 1-year beta),
- 1-year daily beta against SPY (proposal: a satellite below 1.3 should be swapped for a reserve),
- 60-day correlation of daily returns between the student's stocks (pairs above 0.8 flagged).

Nothing is replaced automatically. Replacements are made by editing
configs/students.json and adding a line to that student's "replacements" list
(old, new, date, reason), using the approved reserves (MRVL, LRCX, ANET for satellites).
"""
from __future__ import annotations

import json

import pandas as pd

from . import broker as brokers
from . import clock, paths
from .indicators import clean_bars
from .market_data import get_provider
from .settings import DataCfg, load_strategy, load_students

MIN_BARS = 250
SAT_BETA_FLOOR = 1.3
CORR_LIMIT = 0.8


def validate_symbols() -> dict:
    from .demo import guard
    guard("symbol validation")
    cfg = load_strategy()
    group = load_students()
    provider = get_provider()
    syms = sorted(set(group.all_symbols) | {"SPY"} | set(group.reserves.get("satellite", [])))
    data = provider.daily_bars(syms, DataCfg(history_days=420, feed=cfg.data.feed))
    closes = {}
    counts = {}
    for sym, g in data.bars.groupby("symbol"):
        c, _ = clean_bars(g)
        counts[sym] = len(c)
        closes[sym] = c.set_index("timestamp")["close"]
    px = pd.DataFrame(closes).sort_index()
    rets = px.pct_change(fill_method=None)
    last_year = rets.tail(252)
    spy_var = last_year["SPY"].var() if "SPY" in last_year else None

    def beta(sym):
        if sym not in last_year or not spy_var:
            return None
        pair = last_year[[sym, "SPY"]].dropna()
        if len(pair) < 120:
            return None
        return round(float(pair[sym].cov(pair["SPY"]) / pair["SPY"].var()), 2)

    report = {"checked_at": clock.iso(), "data_source": data.source, "simulated": provider.simulated,
              "batch_requests": data.batch_calls, "students": {},
              "reserves": {r: {"beta_1y": beta(r), "bars": counts.get(r, 0)} for r in group.reserves.get("satellite", [])}}
    for st in group.students:
        rows, flags = [], []
        try:
            b = brokers.get_broker(st)
        except Exception as e:
            b = None
            flags.append(f"cannot reach {st.student_id}'s account: {e}")
        for sym in st.symbols:
            a = {}
            if b is not None:
                try:
                    a = b.asset(sym)
                except Exception as e:
                    a = {"status": f"error: {type(e).__name__}", "tradable": False}
            bt = beta(sym)
            cat = st.category(sym)
            ok = (a.get("status", "").lower() == "active" and a.get("tradable") and counts.get(sym, 0) >= MIN_BARS)
            row = {"symbol": sym, "category": cat, "status": a.get("status"), "tradable": a.get("tradable"),
                   "fractionable": a.get("fractionable"), "bars": counts.get(sym, 0), "beta_1y": bt, "ok": bool(ok)}
            if not ok:
                flags.append(f"{sym}: not usable (status={a.get('status')}, tradable={a.get('tradable')}, "
                             f"bars={counts.get(sym, 0)})")
            if cat == "satellite" and bt is not None and bt < SAT_BETA_FLOOR:
                flags.append(f"{sym}: 1-year beta {bt} is below {SAT_BETA_FLOOR}; the proposal says swap in a reserve")
            rows.append(row)
        corr = rets[st.symbols].tail(60).corr() if all(s in rets for s in st.symbols) else pd.DataFrame()
        pairs = []
        for i, a1 in enumerate(st.symbols):
            for a2 in st.symbols[i + 1:]:
                if not corr.empty and pd.notna(corr.loc[a1, a2]) and corr.loc[a1, a2] > CORR_LIMIT:
                    pairs.append({"pair": f"{a1}/{a2}", "corr_60d": round(float(corr.loc[a1, a2]), 2)})
        for p in pairs:
            flags.append(f"high 60-day correlation {p['pair']} = {p['corr_60d']}")
        report["students"][st.student_id] = {"student": st.label, "symbols": rows, "high_corr_pairs": pairs,
                                             "flags": flags, "all_ok": not any("not usable" in f for f in flags)}

    paths.VALIDATION_DIR.mkdir(parents=True, exist_ok=True)
    stamp = clock.now_utc().strftime("%Y%m%d-%H%M")
    jp = paths.VALIDATION_DIR / f"symbol_validation_{stamp}.json"
    jp.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    md = ["# Symbol validation", "", f"Checked {report['checked_at']} using {data.source} data "
          f"({data.batch_calls} batch request)."]
    if provider.simulated:
        md.append("\n> **SIMULATED DATA** - not a real check.")
    for sid, s in report["students"].items():
        md += ["", f"## {s['student']}", "", "| Symbol | Type | Status | Tradable | Fractionable | Bars | 1y beta |",
               "|---|---|---|---|---|---|---|"]
        md += [f"| {r['symbol']} | {r['category']} | {r['status']} | {r['tradable']} | {r['fractionable']} | "
               f"{r['bars']} | {r['beta_1y']} |" for r in s["symbols"]]
        md += ["", "Flags:"] + ([f"- {f}" for f in s["flags"]] or ["- none"])
    md += ["", "## Reserves", ""] + [f"- {k}: beta {v['beta_1y']}, bars {v['bars']}" for k, v in report["reserves"].items()]
    jp.with_suffix(".md").write_text("\n".join(md) + "\n", encoding="utf-8")
    report["files"] = [str(jp), str(jp.with_suffix(".md"))]
    return report
