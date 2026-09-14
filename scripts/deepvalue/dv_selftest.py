"""
dv_selftest.py — validate the whole pipeline offline with synthetic companies.

`python dv_run.py --selftest` builds a fake 60-name universe containing, by
construction: deep-value turnarounds that should pass, over-levered zombies that
must be cut, serial diluters that must be cut, structurally declining names that
must be cut, and names that never fell (must fail stage 0). If the screener is
wired correctly the passing set is predictable.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

RNG = np.random.default_rng(7)
SECTORS = ["Industrials", "Materials", "Technology", "Healthcare",
           "Consumer Cyclical", "Energy"]


def _path(n: int, drift: float, vol: float, start: float = 100.0) -> np.ndarray:
    r = RNG.normal(drift, vol, n)
    return start * np.exp(np.cumsum(r))


def _price_series(kind: str, n: int = 1010) -> np.ndarray:
    if kind == "beaten":            # long slide, flattening lately
        a = _path(int(n * 0.7), -0.0018, 0.022)
        b = _path(n - len(a), 0.0002, 0.020, a[-1])
        return np.concatenate([a, b])
    if kind == "turning":           # slide then a real reclaim
        a = _path(int(n * 0.72), -0.0020, 0.022)
        b = _path(n - len(a), 0.0022, 0.019, a[-1])
        return np.concatenate([a, b])
    if kind == "never_fell":
        return _path(n, 0.0009, 0.014)
    return _path(n, -0.0012, 0.025)


def _fund(kind: str, ticker: str, sector: str, price: float) -> dict:
    sh = 1.2e8
    mcap = price * sh
    rev = mcap * RNG.uniform(1.2, 3.0)

    base = {
        "ticker": ticker, "ok": True,
        "name": f"{ticker} Corp", "sector": sector, "industry": f"{sector} Sub",
        "market_cap": mcap, "shares_out": sh, "pb": RNG.uniform(0.4, 1.4),
        "short_pct_float": RNG.uniform(0.01, 0.09),
        "held_insiders": RNG.uniform(0.01, 0.12),
        "rev_ttm": rev, "revenue_p1": rev * 0.97,
        "rev_hist": [rev, rev * 0.97, rev * 0.99, rev * 1.02, rev * 1.00, rev * 0.96],
        "ebit_margin_med5": 0.11, "ebit_margin_hist": [0.03, 0.09, 0.12, 0.13, 0.11],
        "gross_profit": rev * 0.31, "gross_profit_p1": rev * 0.295,
        "ebitda": rev * 0.09, "ebit": rev * 0.045,
        "net_income": rev * 0.02, "net_income_p1": rev * 0.005,
        "interest_exp": rev * 0.008,
        "total_assets": rev * 1.1, "total_assets_p1": rev * 1.12, "total_assets_p2": rev * 1.15,
        "cur_assets": rev * 0.45, "cur_assets_p1": rev * 0.43,
        "cur_liab": rev * 0.25, "cur_liab_p1": rev * 0.26,
        "total_debt": rev * 0.22, "lt_debt": rev * 0.18, "lt_debt_p1": rev * 0.21,
        "cash": rev * 0.12, "equity": rev * 0.45, "goodwill_intang": rev * 0.05,
        "inventory": rev * 0.15,
        "shares": sh, "shares_p1": sh * 1.002, "shares_p3": sh * 1.01,
        "cfo": rev * 0.07, "fcf": rev * 0.045, "fcf_avg3": rev * 0.05,
        "capex": -rev * 0.025, "buyback": -rev * 0.01,
        "q_rev": [rev / 4 * x for x in (1.02, 1.00, 0.98, 0.97, 0.99, 0.98, 1.00, 1.01)],
        "q_ni": [rev * 0.006, rev * 0.003, -rev * 0.001, -rev * 0.004,
                 -rev * 0.008, -rev * 0.006, rev * 0.002, rev * 0.004],
        "q_gross_margin": [0.322, 0.312, 0.301, 0.298, 0.295, 0.300],
        "q_op_margin": [0.052, 0.044, 0.038, 0.034, 0.031, 0.036],
        "rev_yoy_q0": 0.031, "rev_yoy_q1": 0.010,
        "q_shares": [sh, sh * 1.001, sh * 1.003, sh * 1.004, sh * 1.006],
        "q_inventory": [rev * 0.14, rev * 0.16, rev * 0.17, rev * 0.16, rev * 0.15],
        "q_cash": [rev * 0.12] * 5, "q_debt": [rev * 0.22] * 5,
        "rev_up_30d": 4, "rev_down_30d": 1,
        "eps_trend_30d_chg": 0.03, "eps_trend_90d_chg": 0.08,
    }

    if kind == "zombie":                       # must be cut: leverage + coverage
        base.update({"total_debt": rev * 1.6, "lt_debt": rev * 1.5,
                     "ebitda": rev * 0.03, "ebit": -rev * 0.02,
                     "interest_exp": rev * 0.09, "cash": rev * 0.02,
                     "cur_assets": rev * 0.18, "cur_liab": rev * 0.30})
    elif kind == "diluter":                    # must be cut: 3Y share growth
        base.update({"shares_p3": sh * 0.55, "shares_p1": sh * 0.90,
                     "q_shares": [sh, sh * 0.97, sh * 0.94, sh * 0.90, sh * 0.86]})
    elif kind == "melting":                    # must be cut: structural decline
        base.update({"rev_hist": [rev, rev * 1.15, rev * 1.32, rev * 1.55,
                                  rev * 1.80, rev * 2.05],
                     "q_gross_margin": [0.19, 0.21, 0.23, 0.25, 0.27, 0.29],
                     "rev_yoy_q0": -0.18, "rev_yoy_q1": -0.12})
    elif kind == "weak_f":                     # near-miss: cheap, safe, not improving
        base.update({"net_income": -rev * 0.03, "net_income_p1": rev * 0.01,
                     "cfo": -rev * 0.01, "fcf_avg3": -rev * 0.005,
                     "gross_profit": rev * 0.26, "gross_profit_p1": rev * 0.30,
                     "lt_debt": rev * 0.24, "lt_debt_p1": rev * 0.20,
                     "cur_assets": rev * 0.40, "cur_assets_p1": rev * 0.44,
                     "q_gross_margin": [0.26, 0.28, 0.29, 0.30, 0.31, 0.31],
                     "q_op_margin": [0.01, 0.02, 0.03, 0.04, 0.04, 0.05],
                     "rev_yoy_q0": -0.05, "rev_yoy_q1": -0.01,
                     "rev_up_30d": 1, "rev_down_30d": 6,
                     "eps_trend_30d_chg": -0.06, "eps_trend_90d_chg": -0.14})
    return base


_MIX = (["turnaround"] * 14 + ["turning"] * 8 + ["zombie"] * 8 + ["diluter"] * 6
        + ["melting"] * 6 + ["weak_f"] * 8 + ["never_fell"] * 10)


def synthetic_injection() -> dict:
    n = 1010
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=n)
    closes, vols, funds, tickers = {}, {}, {}, []

    for i, kind in enumerate(_MIX):
        t = f"T{i:03d}"
        tickers.append(t)
        pk = {"turnaround": "beaten", "turning": "turning", "zombie": "beaten",
              "diluter": "beaten", "melting": "beaten", "weak_f": "beaten",
              "never_fell": "never_fell"}[kind]
        p = _price_series(pk, n)
        closes[t] = pd.Series(p, index=idx)
        base_v = RNG.uniform(6e5, 4e6)
        v = RNG.normal(base_v, base_v * 0.2, n).clip(1e4)
        if kind == "turning":
            v[-20:] *= 2.4
        vols[t] = pd.Series(v, index=idx)
        fk = kind if kind in ("zombie", "diluter", "melting", "weak_f") else "good"
        funds[t] = _fund(fk, t, SECTORS[i % len(SECTORS)], float(p[-1]))

    return {
        "tickers": tickers,
        "prices": {"close": pd.DataFrame(closes), "volume": pd.DataFrame(vols)},
        "fundamentals": funds,
    }
