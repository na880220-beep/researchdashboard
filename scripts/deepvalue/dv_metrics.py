"""
dv_metrics.py — hard cuts, normalized value, Piotroski F-Score, turn score, triggers
====================================================================================

The intellectual centre of the screener is here, and it is three ideas:

1.  In a trough, trailing P/E is useless (denominator is near zero or negative).
    Value must be measured on NORMALIZED earnings power:
        5Y median EBIT margin  x  TTM revenue  /  EV
    plus asset- and cash-based anchors that do not vanish at the bottom.

2.  Cheap + beaten down is a value-trap generator. The one robustly documented
    enhancement (Piotroski 2000) is to require evidence that fundamentals are
    ALREADY improving. F-Score is the gate, not a tiebreaker.

3.  Survival and dilution are BINARY, not scored. A company that cannot fund
    itself for two years cannot deliver a two-year turn, and one that funds
    itself by issuing stock hands the recovery to the new holders.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from dv_core import Config, CFG


def _f(x) -> float:
    try:
        v = float(x)
        return v if np.isfinite(v) else np.nan
    except (TypeError, ValueError):
        return np.nan


def _safe_div(a, b) -> float:
    a, b = _f(a), _f(b)
    if not np.isfinite(a) or not np.isfinite(b) or abs(b) < 1e-9:
        return np.nan
    return a / b


# =============================================================================
# DERIVED FUNDAMENTAL METRICS
# =============================================================================

def derive_metrics(fd: dict, px: dict) -> dict:
    """Combine one ticker's fundamentals + price features into flat metrics."""
    m: dict = {}
    mcap = _f(fd.get("market_cap"))
    debt = _f(fd.get("total_debt"))
    cash = _f(fd.get("cash"))
    if not np.isfinite(debt):
        debt = _f(fd.get("lt_debt"))
    net_debt = (debt if np.isfinite(debt) else 0.0) - (cash if np.isfinite(cash) else 0.0)
    ev = mcap + net_debt if np.isfinite(mcap) else _f(fd.get("ev_info"))

    m["market_cap"] = mcap
    m["ev"] = ev
    m["net_debt"] = net_debt
    m["net_cash_position"] = bool(np.isfinite(net_debt) and net_debt < 0)

    rev = _f(fd.get("rev_ttm"))
    ebitda = _f(fd.get("ebitda"))
    ebit = _f(fd.get("ebit")) if "ebit" in fd else np.nan
    ni = _f(fd.get("net_income"))
    equity = _f(fd.get("equity"))
    ta = _f(fd.get("total_assets"))
    gi = _f(fd.get("goodwill_intang"))
    cfo = _f(fd.get("cfo"))

    # ---- normalized earnings power (the core value metric) -----------------
    med_m = _f(fd.get("ebit_margin_med5"))
    norm_ebit = med_m * rev if np.isfinite(med_m) and np.isfinite(rev) else np.nan
    m["ebit_margin_med5"] = med_m
    m["norm_ebit"] = norm_ebit
    m["norm_earn_yield"] = _safe_div(norm_ebit, ev)
    m["norm_pe_proxy"] = _safe_div(ev, norm_ebit)

    # ---- conventional anchors ----------------------------------------------
    m["ev_sales"] = _safe_div(ev, rev)
    m["ev_ebitda"] = _safe_div(ev, ebitda) if np.isfinite(ebitda) and ebitda > 0 else np.nan
    m["fcf_yield"] = _safe_div(_f(fd.get("fcf_avg3")), ev)
    m["pb"] = _f(fd.get("pb"))
    tang_bv = equity - gi if np.isfinite(equity) and np.isfinite(gi) else equity
    m["tangible_bv"] = tang_bv
    m["p_tangible_bv"] = _safe_div(mcap, tang_bv) if np.isfinite(tang_bv) and tang_bv > 0 else np.nan
    m["ncav"] = (_f(fd.get("cur_assets")) - (_f(fd.get("cur_liab")) or 0)
                 - (debt if np.isfinite(debt) else 0))
    m["p_ncav"] = _safe_div(mcap, m["ncav"]) if np.isfinite(m["ncav"]) and m["ncav"] > 0 else np.nan

    # ---- survival -----------------------------------------------------------
    ie = abs(_f(fd.get("interest_exp"))) if np.isfinite(_f(fd.get("interest_exp"))) else np.nan
    m["interest_coverage"] = (np.inf if (not np.isfinite(ie) or ie < 1e-6)
                              else _safe_div(ebit if np.isfinite(ebit) else norm_ebit, ie))
    m["net_debt_ebitda"] = (-0.5 if m["net_cash_position"]
                            else (_safe_div(net_debt, ebitda) if np.isfinite(ebitda) and ebitda > 0 else np.nan))
    m["current_ratio"] = _safe_div(_f(fd.get("cur_assets")), _f(fd.get("cur_liab")))
    m["equity_positive"] = bool(np.isfinite(equity) and equity > 0)

    # ---- dilution ------------------------------------------------------------
    sh0, sh3 = _f(fd.get("shares")), _f(fd.get("shares_p3"))
    m["share_growth_3y"] = _safe_div(sh0, sh3) - 1 if np.isfinite(sh0) and np.isfinite(sh3) else np.nan
    qs = fd.get("q_shares") or []
    m["share_chg_qoq"] = _safe_div(qs[0], qs[1]) - 1 if len(qs) >= 2 else np.nan
    m["buyback_active"] = bool(_f(fd.get("buyback")) < 0) if np.isfinite(_f(fd.get("buyback"))) else False

    # ---- structural decline test ---------------------------------------------
    rh = [x for x in (fd.get("rev_hist") or []) if np.isfinite(_f(x)) and x > 0]
    if len(rh) >= 4:
        yrs = len(rh) - 1
        m["rev_cagr_5y"] = (rh[0] / rh[-1]) ** (1 / yrs) - 1
    else:
        m["rev_cagr_5y"] = np.nan

    # ---- trough evidence ------------------------------------------------------
    gm = fd.get("q_gross_margin") or []
    m["gm_q0"] = gm[0] if gm else np.nan
    m["gm_improving_2q"] = bool(len(gm) >= 3 and gm[0] > gm[1] > gm[2])
    m["gm_improving_1q"] = bool(len(gm) >= 2 and gm[0] > gm[1])
    om = fd.get("q_op_margin") or []
    m["om_q0"] = om[0] if om else np.nan
    m["om_improving_2q"] = bool(len(om) >= 3 and om[0] > om[1] > om[2])

    yoy0, yoy1 = _f(fd.get("rev_yoy_q0")), _f(fd.get("rev_yoy_q1"))
    m["rev_yoy_q0"] = yoy0
    m["rev_yoy_inflect"] = bool(np.isfinite(yoy0) and np.isfinite(yoy1) and yoy0 > yoy1)

    qni = fd.get("q_ni") or []
    m["loss_narrowing"] = bool(len(qni) >= 5 and qni[0] < 0 and qni[4] < 0 and qni[0] > qni[4])
    m["swung_to_profit"] = bool(len(qni) >= 5 and qni[0] > 0 and qni[4] < 0)

    qinv, qrev = fd.get("q_inventory") or [], fd.get("q_rev") or []
    if len(qinv) >= 2 and len(qrev) >= 2 and qrev[0] > 0 and qrev[1] > 0:
        m["inv_to_sales_chg"] = (qinv[0] / qrev[0]) - (qinv[1] / qrev[1])
    else:
        m["inv_to_sales_chg"] = np.nan

    # ---- revisions -------------------------------------------------------------
    up, dn = _f(fd.get("rev_up_30d")), _f(fd.get("rev_down_30d"))
    m["rev_up_30d"], m["rev_down_30d"] = up, dn
    m["revision_breadth"] = (_safe_div(up - dn, up + dn)
                             if np.isfinite(up) and np.isfinite(dn) and (up + dn) > 0 else np.nan)
    m["eps_trend_30d_chg"] = _f(fd.get("eps_trend_30d_chg"))
    m["eps_trend_90d_chg"] = _f(fd.get("eps_trend_90d_chg"))

    m.update(px)
    m["name"] = fd.get("name")
    m["sector"] = fd.get("sector") or "Unknown"
    m["industry"] = fd.get("industry") or "Unknown"
    m["short_pct_float"] = _f(fd.get("short_pct_float"))
    m["held_insiders"] = _f(fd.get("held_insiders"))
    return m


# =============================================================================
# PIOTROSKI F-SCORE
# =============================================================================

def piotroski(fd: dict) -> tuple[float, dict]:
    ta0, ta1, ta2 = _f(fd.get("total_assets")), _f(fd.get("total_assets_p1")), _f(fd.get("total_assets_p2"))
    ni0, ni1 = _f(fd.get("net_income")), _f(fd.get("net_income_p1"))
    cfo0 = _f(fd.get("cfo"))
    roa0, roa1 = _safe_div(ni0, ta1), _safe_div(ni1, ta2)

    c: dict = {}
    c["f1_roa_pos"] = 1 if np.isfinite(roa0) and roa0 > 0 else 0
    c["f2_cfo_pos"] = 1 if np.isfinite(cfo0) and cfo0 > 0 else 0
    c["f3_droa_pos"] = 1 if np.isfinite(roa0) and np.isfinite(roa1) and roa0 > roa1 else 0
    c["f4_accrual"] = 1 if np.isfinite(_safe_div(cfo0, ta1)) and np.isfinite(roa0) and _safe_div(cfo0, ta1) > roa0 else 0

    lev0 = _safe_div(_f(fd.get("lt_debt")), ta0)
    lev1 = _safe_div(_f(fd.get("lt_debt_p1")), ta1)
    c["f5_dlev_down"] = 1 if np.isfinite(lev0) and np.isfinite(lev1) and lev0 < lev1 else 0

    cr0 = _safe_div(_f(fd.get("cur_assets")), _f(fd.get("cur_liab")))
    cr1 = _safe_div(_f(fd.get("cur_assets_p1")), _f(fd.get("cur_liab_p1")))
    c["f6_dcurr_up"] = 1 if np.isfinite(cr0) and np.isfinite(cr1) and cr0 > cr1 else 0

    sh0, sh1 = _f(fd.get("shares")), _f(fd.get("shares_p1"))
    c["f7_no_issue"] = 1 if np.isfinite(sh0) and np.isfinite(sh1) and sh0 <= sh1 * 1.01 else 0

    gm0 = _safe_div(_f(fd.get("gross_profit")), _f(fd.get("rev_ttm")))
    gm1 = _safe_div(_f(fd.get("gross_profit_p1")), _f(fd.get("revenue_p1")))
    c["f8_dmargin_up"] = 1 if np.isfinite(gm0) and np.isfinite(gm1) and gm0 > gm1 else 0

    at0 = _safe_div(_f(fd.get("rev_ttm")), ta1)
    at1 = _safe_div(_f(fd.get("revenue_p1")), ta2)
    c["f9_dturnover_up"] = 1 if np.isfinite(at0) and np.isfinite(at1) and at0 > at1 else 0

    return float(sum(c.values())), c


# =============================================================================
# STAGE 0 / STAGE 1 GATES
# =============================================================================

def stage0_pass(px: dict, mcap: float | None, cfg: Config = CFG) -> tuple[bool, str]:
    if px is None:
        return False, "no_price_history"
    if px["price"] < cfg.min_price:
        return False, "price_too_low"
    if px["dollar_vol_20d"] < cfg.min_dollar_vol_20d:
        return False, "illiquid"
    if px["dd_3y_high"] > cfg.dd_from_3y_high:
        return False, "not_drawn_down_enough"
    if px["days_below_200dma_1y"] < cfg.min_days_below_200dma:
        return False, "not_neglected_long_enough"
    if np.isfinite(px.get("dist_200dma", np.nan)) and px["dist_200dma"] > cfg.max_dist_above_200dma:
        return False, "already_recovered"
    if mcap is not None and np.isfinite(_f(mcap)):
        if _f(mcap) < cfg.min_market_cap:
            return False, "mcap_too_small"
        if _f(mcap) > cfg.max_market_cap:
            return False, "mcap_too_large"
    return True, ""


def stage1_pass(m: dict, cfg: Config = CFG) -> tuple[bool, str]:
    """Survival + dilution + structural decline. Binary. No partial credit."""
    reasons = []
    ic = m.get("interest_coverage")
    if np.isfinite(_f(ic)) and ic < cfg.min_interest_coverage and not m.get("net_cash_position"):
        reasons.append("interest_coverage")
    nde = _f(m.get("net_debt_ebitda"))
    if np.isfinite(nde) and nde > cfg.max_net_debt_ebitda:
        reasons.append("leverage")
    cr = _f(m.get("current_ratio"))
    if np.isfinite(cr) and cr < cfg.min_current_ratio:
        reasons.append("liquidity")
    if not m.get("equity_positive") and not (cfg.allow_negative_equity_if_net_cash and m.get("net_cash_position")):
        reasons.append("negative_equity")
    sg = _f(m.get("share_growth_3y"))
    if np.isfinite(sg) and sg > cfg.max_share_growth_3y:
        reasons.append("dilution")
    cg = _f(m.get("rev_cagr_5y"))
    if np.isfinite(cg) and cg < cfg.min_rev_cagr_5y:
        reasons.append("structural_decline")
    return (len(reasons) == 0), "|".join(reasons)


# =============================================================================
# SCORING
# =============================================================================

def _pctile(s: pd.Series, higher_is_better: bool = True) -> pd.Series:
    r = s.rank(pct=True, na_option="keep")
    return r if higher_is_better else 1 - r


def score_frame(df: pd.DataFrame, cfg: Config = CFG) -> pd.DataFrame:
    """Cross-sectional percentile scoring. Ranked WITHIN sector where possible
    to stop the whole list becoming one sector's cycle trough."""
    d = df.copy()

    val_specs = [
        ("norm_earn_yield", True, 0.30),
        ("fcf_yield", True, 0.25),
        ("ev_sales", False, 0.15),
        ("p_tangible_bv", False, 0.15),
        ("ev_ebitda", False, 0.15),
    ]
    turn_specs = [
        ("fscore", True, 0.35),
        ("revision_breadth", True, 0.15),
        ("eps_trend_90d_chg", True, 0.15),
        ("rev_yoy_q0", True, 0.10),
        ("gm_q0", True, 0.05),
    ]

    def _weighted(specs) -> pd.Series:
        tot = pd.Series(0.0, index=d.index)
        wsum = pd.Series(0.0, index=d.index)
        for col, hib, w in specs:
            if col not in d.columns:
                continue
            p = _pctile(pd.to_numeric(d[col], errors="coerce"), hib)
            ok = p.notna()
            tot[ok] += p[ok] * w
            wsum[ok] += w
        return (tot / wsum.replace(0, np.nan)).fillna(0.0)

    d["value_score"] = _weighted(val_specs)
    turn_base = _weighted(turn_specs)

    bonus = pd.Series(0.0, index=d.index)
    for col, pts in [("gm_improving_2q", 0.06), ("om_improving_2q", 0.06),
                     ("rev_yoy_inflect", 0.05), ("swung_to_profit", 0.06),
                     ("loss_narrowing", 0.03), ("buyback_active", 0.04),
                     ("net_cash_position", 0.04)]:
        if col in d.columns:
            bonus += d[col].fillna(False).infer_objects(copy=False).astype(bool).astype(float) * pts
    d["turn_score"] = (turn_base + bonus).clip(0, 1)

    d["composite"] = cfg.w_value * d["value_score"] + cfg.w_turn * d["turn_score"]

    # sector-relative rank so one blown-up sector cannot own the entire list
    d["sector_rank"] = d.groupby("sector")["composite"].rank(ascending=False, method="min")
    d["overall_rank"] = d["composite"].rank(ascending=False, method="min")
    return d.sort_values("composite", ascending=False)


# =============================================================================
# DAILY TRIGGERS
# =============================================================================

TRIGGER_COLS = [
    "trig_reclaim_200dma", "trig_vol_surge", "trig_sma50_turn",
    "trig_revision_flip", "trig_margin_trough", "trig_new_entrant",
    "trig_buyback",
]


def apply_triggers(d: pd.DataFrame, prev: pd.DataFrame | None, cfg: Config = CFG) -> pd.DataFrame:
    d = d.copy()
    d["trig_vol_surge"] = (
        (pd.to_numeric(d.get("vol_ratio_20_60"), errors="coerce") > cfg.trig_vol_surge_ratio)
        & (pd.to_numeric(d.get("ret_5d"), errors="coerce") > cfg.trig_vol_surge_ret5)
    ).fillna(False)

    d["trig_sma50_turn"] = (pd.to_numeric(d.get("sma50_slope_20d"), errors="coerce") > 0).fillna(False)

    rb = pd.to_numeric(d.get("revision_breadth"), errors="coerce")
    et = pd.to_numeric(d.get("eps_trend_30d_chg"), errors="coerce")
    d["trig_revision_flip"] = ((rb > 0) | (et > 0.01)).fillna(False)

    def _bool_col(name: str) -> pd.Series:
        if name not in d.columns:
            return pd.Series(False, index=d.index)
        return d[name].fillna(False).astype(bool)

    d["trig_margin_trough"] = _bool_col("gm_improving_2q") | _bool_col("om_improving_2q")
    d["trig_buyback"] = _bool_col("buyback_active")

    if prev is not None and "ticker" in prev.columns:
        seen = set(prev["ticker"].astype(str))
        d["trig_new_entrant"] = ~d["ticker"].astype(str).isin(seen)
        pm = prev.set_index("ticker")
        if "fscore" in pm.columns:
            d["fscore_prev"] = d["ticker"].map(pm["fscore"])
            d["fscore_delta"] = pd.to_numeric(d["fscore"], errors="coerce") - pd.to_numeric(d["fscore_prev"], errors="coerce")
        if "composite" in pm.columns:
            d["composite_prev"] = d["ticker"].map(pm["composite"])
            d["composite_delta"] = d["composite"] - pd.to_numeric(d["composite_prev"], errors="coerce")
    else:
        d["trig_new_entrant"] = False
        d["fscore_delta"] = np.nan
        d["composite_delta"] = np.nan

    for c in TRIGGER_COLS:
        if c not in d.columns:
            d[c] = False
        d[c] = d[c].fillna(False).astype(bool)

    d["trigger_count"] = d[TRIGGER_COLS].sum(axis=1)
    d["triggers"] = d[TRIGGER_COLS].apply(
        lambda r: ", ".join(c.replace("trig_", "") for c in TRIGGER_COLS if r[c]), axis=1
    )
    return d
