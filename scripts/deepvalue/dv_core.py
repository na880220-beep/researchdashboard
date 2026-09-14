"""
dv_core.py — US Deep-Value / Turnaround Screener :: data + config layer
=======================================================================
Universe: NDX (Nasdaq-100), SPX (S&P 500), R2K (Russell 2000 via IWM holdings)

Design principle
----------------
Valuation and drawdown are MONTHLY variables. Recomputing them daily returns
yesterday's list. What actually changes daily is (a) price/volume structure and
(b) analyst revisions / insider activity. So this screener splits into:

    STAGE 0  price + liquidity prefilter        (bulk, fast, every run)
    STAGE 1  survival + dilution HARD CUTS      (fundamentals, cached 7d)
    STAGE 2  normalized value metrics           (cached)
    STAGE 3  Piotroski F-Score                  (cached)
    STAGE 4  composite rank
    STAGE 5  DAILY TRIGGERS  <-- the part you actually read each morning

Only Stage 0 survivors get fundamentals pulled. That is what makes a
~2,600-name universe run in minutes instead of hours.

Author's note: every threshold lives in CFG. Nothing is hardcoded downstream.
"""

from __future__ import annotations

import io
import json
import re
import os
import time
import warnings
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

try:
    import yfinance as yf
except ImportError:  # pragma: no cover
    yf = None


# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass
class Config:
    # ---- Stage 0 : price / liquidity / neglect -----------------------------
    min_price: float = 3.0                 # sub-$3 US names are structurally junky
    min_dollar_vol_20d: float = 2_000_000  # must be exitable
    min_market_cap: float = 200_000_000
    max_market_cap: float = 500_000_000_000

    dd_from_3y_high: float = -0.45         # "처박혀 있음" : >=45% off 3Y high
    min_days_below_200dma: int = 100       # ...and off it for a LONG time (of 252)
    max_dist_above_200dma: float = 0.25    # not already fully recovered

    # ---- Stage 1 : survival hard cuts (binary exclusion, no scoring) -------
    min_interest_coverage: float = 1.5     # EBIT / |interest expense|
    max_net_debt_ebitda: float = 4.0
    min_current_ratio: float = 1.0
    max_share_growth_3y: float = 0.15      # 15% dilution over 3Y = disqualified
    min_rev_cagr_5y: float = -0.05         # structural decline filter
    allow_negative_equity_if_net_cash: bool = True

    # ---- Stage 3 : quality gate -------------------------------------------
    min_fscore: int = 6                    # Piotroski, 0-9

    # ---- Stage 4 : composite weights --------------------------------------
    w_value: float = 0.55
    w_turn: float = 0.45
    top_n_per_universe: int = 60

    # ---- Stage 5 : daily triggers -----------------------------------------
    trig_vol_surge_ratio: float = 1.8      # vol20 / vol60
    trig_vol_surge_ret5: float = 0.02      # ...with price confirming
    trig_reclaim_lookback: int = 10        # 200DMA reclaim within N sessions

    # ---- plumbing ----------------------------------------------------------
    cache_dir: str = "./dv_cache"
    snapshot_dir: str = "./dv_snapshots"
    fundamentals_ttl_days: int = 7         # financials update quarterly
    max_workers: int = 8
    throttle_sec: float = 0.12             # be polite to the endpoint
    price_batch: int = 200

    def dump(self) -> pd.DataFrame:
        rows = [{"parameter": k, "value": v} for k, v in asdict(self).items()]
        return pd.DataFrame(rows)


CFG = Config()


# =============================================================================
# UNIVERSE CONSTRUCTION
# =============================================================================

_WIKI_SPX = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_WIKI_NDX = "https://en.wikipedia.org/wiki/Nasdaq-100"
_MASTER = ("https://raw.githubusercontent.com/rreichel3/US-Stock-Symbols/"
           "main/{ex}/{ex}_full_tickers.json")
_EXCHANGES = ("nasdaq", "nyse", "amex")
_SMALLCAP_MAX = 1.0e10          # upper bound of the Russell-2000-equivalent cell
_WIKI_API = "https://en.wikipedia.org/w/api.php?action=parse&page={}&prop=text&format=json"
_SLICK_SPX = "https://www.slickcharts.com/sp500"
_SLICK_NDX = "https://www.slickcharts.com/nasdaq100"
_GH_SPX = ("https://raw.githubusercontent.com/datasets/s-and-p-500-companies/"
           "main/data/constituents.csv")
_QQQ_CSV = ("https://www.invesco.com/us/financial-products/etfs/holdings/main/"
            "holdings/0?audienceType=Investor&action=download&ticker=QQQ")
_IWM_CSV = (
    "https://www.ishares.com/us/products/239710/"
    "ishares-russell-2000-etf/1467271812596.ajax"
    "?fileType=csv&fileName=IWM_holdings&dataType=fund"
)

# Wikipedia (and most data hosts) reject requests that do not identify
# themselves. pandas.read_html sends no User-Agent, so it gets HTTP 403 —
# every network fetch in this module goes through _get() instead.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")


def _get(url: str, tries: int = 3, timeout: int = 60):
    """GET with a browser-like identity.

    Two failure modes seen in the wild, both fixed here:
      * no User-Agent      -> Wikipedia returns 403 Forbidden
      * narrow Accept list  -> Invesco returns 406 Not Acceptable
    So Accept is '*/*' (accept anything) rather than an explicit type list.
    """
    import requests

    headers = {
        "User-Agent": _UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    }
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, timeout=timeout, headers=headers)
            r.raise_for_status()
            return r
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"fetch failed after {tries} tries: {url}\n  {last}")


def _clean_ticker(t: str) -> str:
    return str(t).strip().upper().replace(".", "-")


def _tickers_from(series) -> list[str]:
    bad = {"-", "USD", "CASH", "NAN", ""}
    out = {_clean_ticker(t) for t in series.dropna()}
    return sorted(t for t in out if t not in bad and len(t) <= 6)


def _holdings_csv(url: str, col_candidates=("Ticker", "Holding Ticker", "Symbol")) -> list[str]:
    """Parse an ETF holdings CSV that has preamble rows before the header."""
    txt = _get(url).text
    start = -1
    for c in col_candidates:
        start = txt.find(c + ",")
        if start != -1:
            break
    if start == -1:
        raise RuntimeError("holdings layout changed (header row not found)")
    df = pd.read_csv(io.StringIO(txt[start:]), on_bad_lines="skip")
    if "Asset Class" in df.columns:
        df = df[df["Asset Class"].astype(str).str.contains("Equity", na=False)]
    for c in col_candidates:
        if c in df.columns:
            return _tickers_from(df[c])
    raise RuntimeError("holdings layout changed (ticker column not found)")


_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9]{0,4}([.-][A-Z])?$")


def _looks_like_tickers(col: pd.Series) -> bool:
    v = col.dropna().astype(str).str.strip().str.upper()
    if len(v) < 50:
        return False
    return (v.map(lambda x: bool(_TICKER_RE.match(x))).mean()) > 0.8


def _table_tickers(tables) -> list[str]:
    """Find the ticker column without depending on its exact header text.

    Wikipedia renames headers ('Ticker' -> 'Ticker symbol' etc.), which is what
    broke the previous version. Match on the header loosely first, then fall
    back to detecting a column whose VALUES look like tickers.
    """
    for tbl in tables:
        for c in tbl.columns:
            if re.search(r"tick|symbol", str(c), re.I):
                t = _tickers_from(tbl[c])
                if len(t) > 50:
                    return t
    for tbl in tables:                      # header gave nothing: sniff values
        for c in tbl.columns:
            if _looks_like_tickers(tbl[c]):
                t = _tickers_from(tbl[c])
                if len(t) > 50:
                    return t
    raise RuntimeError("no ticker column found in any table")


def _wiki_table(url: str) -> list[str]:
    return _table_tickers(pd.read_html(io.StringIO(_get(url).text)))


def _wiki_api(page: str) -> list[str]:
    """Wikipedia's parse API rather than the rendered page.

    api.php is the endpoint Wikipedia actually intends for programmatic use, so
    it is far less likely to be blocked or restructured than scraping HTML.
    """
    data = _get(_WIKI_API.format(page)).json()
    html = data["parse"]["text"]["*"]
    return _table_tickers(pd.read_html(io.StringIO(html)))


def _slickcharts(url: str) -> list[str]:
    return _table_tickers(pd.read_html(io.StringIO(_get(url).text)))


def get_master(cfg: Config = CFG) -> pd.DataFrame:
    """The full US listed-security master, with market cap and sector.

    Index membership (Russell 2000 especially) has no dependable free source —
    every provider either blocks automated access or restructures its page. But
    membership is not actually what this screener needs: it needs liquid US
    operating companies in a size band, and that comes straight from the
    exchange listing files. Served from raw.githubusercontent.com, which does
    not block.
    """
    cache = os.path.join(cfg.cache_dir, "universes", "security_master.csv")
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    if _cache_fresh(cache, 7):
        return pd.read_csv(cache)

    rows = []
    for ex in _EXCHANGES:
        try:
            data = _get(_MASTER.format(ex=ex)).json()
            for r in data:
                r["exchange"] = ex.upper()
            rows += data
            print(f"    [master] {ex}: {len(data)} listings")
        except Exception as exc:  # noqa: BLE001
            print(f"    [master] {ex} failed ({str(exc)[:70]})")

    if not rows:
        if os.path.exists(cache):
            print("    [master] live fetch failed; using cached security master")
            return pd.read_csv(cache)
        raise RuntimeError("security master unavailable from every exchange file")

    df = pd.DataFrame(rows)
    df["mcap"] = pd.to_numeric(df.get("marketCap"), errors="coerce")
    df["px"] = pd.to_numeric(
        df.get("lastsale").astype(str).str.replace(r"[$,]", "", regex=True), errors="coerce")
    df["sector"] = df.get("sector").fillna("").astype(str).str.strip()
    df["symbol"] = df["symbol"].astype(str).str.strip().str.upper()

    # Blank sector marks ETFs, closed-end funds and trusts; the 5-char W/U/R/P/Q
    # suffixes mark warrants, SPAC units, rights, preferreds and bankrupt issues.
    def _is_common(sym: str) -> bool:
        if not _TICKER_RE.match(sym):
            return False
        return not (len(sym) == 5 and sym[-1] in "WURPQ")

    df = df[df["symbol"].map(_is_common) & (df["sector"] != "")]
    df = df.drop_duplicates(subset="symbol")
    df.to_csv(cache, index=False)
    print(f"    [master] {len(df)} operating companies after cleaning")
    return df


def get_smallcap(cfg: Config = CFG) -> list[str]:
    """Small/mid-cap cell — the Russell 2000 stand-in, built by size not membership."""
    df = get_master(cfg)
    m = df[(df["mcap"] >= cfg.min_market_cap) & (df["mcap"] < _SMALLCAP_MAX)
           & (df["px"] >= cfg.min_price)]
    return _tickers_from(m["symbol"])


def get_largecap(cfg: Config = CFG) -> list[str]:
    df = get_master(cfg)
    m = df[(df["mcap"] >= _SMALLCAP_MAX) & (df["mcap"] <= cfg.max_market_cap)
           & (df["px"] >= cfg.min_price)]
    return _tickers_from(m["symbol"])


def get_spx() -> list[str]:
    for label, fn in (("github csv", lambda: _tickers_from(
                          pd.read_csv(io.StringIO(_get(_GH_SPX).text))["Symbol"])),
                      ("wikipedia api", lambda: _wiki_api("List_of_S%26P_500_companies")),
                      ("wikipedia page", lambda: _wiki_table(_WIKI_SPX)),
                      ("slickcharts", lambda: _slickcharts(_SLICK_SPX))):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            print(f"    [spx] {label} failed ({str(exc)[:90]})")
    raise RuntimeError("SPX: every source failed — pass your own list via --universe-file")


def get_ndx() -> list[str]:
    for label, fn in (("wikipedia api", lambda: _wiki_api("Nasdaq-100")),
                      ("wikipedia page", lambda: _wiki_table(_WIKI_NDX)),
                      ("slickcharts", lambda: _slickcharts(_SLICK_NDX)),
                      ("QQQ holdings", lambda: _holdings_csv(_QQQ_CSV))):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            print(f"    [ndx] {label} failed ({str(exc)[:90]})")
    raise RuntimeError("NDX: every source failed — pass your own list via --universe-file")


def get_r2k() -> list[str]:
    return _holdings_csv(_IWM_CSV)


def _universe_cache(name: str, cfg: Config) -> str:
    d = os.path.join(cfg.cache_dir, "universes")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{name}.csv")


def load_universe(name: str, universe_file: str | None = None,
                  cfg: Config = CFG) -> list[str]:
    """Resolve an index to its constituents, with a durable on-disk fallback.

    Index membership changes only at quarterly rebalances, so scraping it on
    every run is all downside: three separate outages here (403, 406, a header
    rename) each stopped the screener from starting. Once ANY source succeeds
    the list is cached; if every source is later blocked, the cached list is
    used and its age reported rather than aborting the run.
    """
    if universe_file:
        return _tickers_from(pd.read_csv(universe_file).iloc[:, 0])

    builders = {"ndx": get_ndx, "spx": get_spx, "r2k": get_r2k,
                "smallcap": lambda: get_smallcap(cfg),
                "largecap": lambda: get_largecap(cfg)}
    fn = builders.get(name.lower())
    if fn is None:
        raise ValueError(f"unknown universe {name!r}; "
                         f"use one of {sorted(builders)} or a universe file")

    path = _universe_cache(name, cfg)
    try:
        tickers = fn()
        if len(tickers) < 20:
            raise RuntimeError(f"only {len(tickers)} tickers returned")
        pd.DataFrame({"ticker": tickers}).to_csv(path, index=False)
        return tickers
    except Exception as exc:  # noqa: BLE001
        if os.path.exists(path):
            age = (time.time() - os.path.getmtime(path)) / 86400
            tickers = _tickers_from(pd.read_csv(path)["ticker"])
            print(f"    [{name}] all live sources failed; using cached list "
                  f"({len(tickers)} tickers, {age:.0f}일 전 저장)")
            if age > 100:
                print(f"    [{name}] 경고: 목록이 {age:.0f}일 지났습니다. "
                      f"분기 리밸런싱이 반영되지 않았을 수 있습니다.")
            return tickers
        raise RuntimeError(
            f"{name.upper()}: 모든 출처 실패, 캐시도 없습니다.\n"
            f"  → 티커 CSV를 만들어 universe_file로 지정하거나, "
            f"이 유니버스를 건너뛰세요.\n  마지막 오류: {exc}") from exc


def _extract_chunk(df, chunk: list[str]):
    """Pull (close, volume) out of a yf.download result with CORRECT labels.

    yfinance changes its column layout depending on how many tickers actually
    resolved. Assuming the surviving column belongs to chunk[0] silently
    mislabels one company's prices as another's, so an ambiguous layout returns
    (None, None) and the caller re-fetches that chunk one ticker at a time.
    """
    if df is None or df.empty:
        return None, None
    if isinstance(df.columns, pd.MultiIndex):
        lv0 = set(df.columns.get_level_values(0))
        if "Close" in lv0:                       # group_by='column'
            return df["Close"], df["Volume"]
        try:                                     # group_by='ticker'
            return df.xs("Close", axis=1, level=1), df.xs("Volume", axis=1, level=1)
        except KeyError:
            return None, None
    if len(chunk) == 1 and "Close" in df.columns:
        t = chunk[0]
        return (df[["Close"]].rename(columns={"Close": t}),
                df[["Volume"]].rename(columns={"Volume": t}))
    return None, None


def _download(chunk: list[str], period: str, tries: int = 3):
    last = None
    for i in range(tries):
        try:
            return yf.download(chunk, period=period, interval="1d", auto_adjust=True,
                               progress=False, threads=True, group_by="column")
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(3 * (i + 1))              # back off; usually rate limiting
    print(f"    [warn] download failed for {len(chunk)} tickers: {last}")
    return None


def fetch_prices(tickers: list[str], period: str = "4y", cfg: Config = CFG) -> dict:
    """Return {'close': DataFrame, 'volume': DataFrame} indexed by date.

    A failing chunk is retried, then split per-ticker, then skipped — one bad
    ticker in a 2,000-name universe must not abort the whole run.
    """
    closes, volumes, failed = [], [], []
    batch = cfg.price_batch if len(tickers) <= 600 else 100
    total = len(tickers)
    t0 = time.time()

    for i in range(0, total, batch):
        chunk = tickers[i : i + batch]
        c, v = _extract_chunk(_download(chunk, period), chunk)

        if c is None and len(chunk) > 1:         # ambiguous layout -> per ticker
            print(f"    [warn] chunk layout unclear; retrying {len(chunk)} tickers individually")
            for t in chunk:
                c1, v1 = _extract_chunk(_download([t], period, tries=2), [t])
                if c1 is None:
                    failed.append(t)
                else:
                    closes.append(c1)
                    volumes.append(v1)
        elif c is None:
            failed.extend(chunk)
        else:
            closes.append(c)
            volumes.append(v)

        done = min(i + batch, total)
        el = time.time() - t0
        eta = el / max(done, 1) * (total - done)
        print(f"    prices {done}/{total}  ({el/60:.1f}m elapsed, ~{eta/60:.1f}m left)")
        time.sleep(0.4)

    if not closes:
        raise RuntimeError(
            "no price data returned at all — network blocked or yfinance rate limited. "
            "Wait a few minutes and re-run; cached fundamentals are preserved.")

    close = pd.concat(closes, axis=1).sort_index()
    volume = pd.concat(volumes, axis=1).sort_index()
    close = close.loc[:, ~close.columns.duplicated()]
    volume = volume.loc[:, ~volume.columns.duplicated()]

    got = close.notna().any().sum()
    print(f"    usable price series: {got}/{total}"
          + (f"  (no data: {len(failed)})" if failed else ""))
    return {"close": close, "volume": volume}


def price_features(close: pd.Series, volume: pd.Series) -> dict | None:
    """All Stage-0 and trigger-relevant price structure for one name."""
    c = close.dropna()
    if len(c) < 260:
        return None
    v = volume.reindex(c.index).fillna(0)

    sma200 = c.rolling(200).mean()
    sma50 = c.rolling(50).mean()
    last = float(c.iloc[-1])

    win3y = c.iloc[-756:] if len(c) >= 756 else c
    high3y = float(win3y.max())
    win1y = c.iloc[-252:]

    below = (c.iloc[-252:] < sma200.iloc[-252:])
    days_below = int(below.sum())

    dv20 = float((c.iloc[-20:] * v.iloc[-20:]).mean())
    vol20 = float(v.iloc[-20:].mean())
    vol60 = float(v.iloc[-60:].mean())

    s200 = sma200.iloc[-CFG.trig_reclaim_lookback - 1 :]
    c_tail = c.iloc[-CFG.trig_reclaim_lookback - 1 :]
    above_tail = (c_tail > s200)
    reclaimed = bool(above_tail.iloc[-1] and not above_tail.iloc[0])

    return {
        "price": last,
        "dd_3y_high": last / high3y - 1.0,
        "dd_52w_high": last / float(win1y.max()) - 1.0,
        "off_52w_low": last / float(win1y.min()) - 1.0,
        "dist_200dma": last / float(sma200.iloc[-1]) - 1.0 if sma200.iloc[-1] > 0 else np.nan,
        "days_below_200dma_1y": days_below,
        "sma50_slope_20d": float(sma50.iloc[-1] / sma50.iloc[-21] - 1.0) if len(sma50.dropna()) > 21 else np.nan,
        "dollar_vol_20d": dv20,
        "vol_ratio_20_60": vol20 / vol60 if vol60 > 0 else np.nan,
        "ret_5d": float(c.iloc[-1] / c.iloc[-6] - 1.0),
        "ret_21d": float(c.iloc[-1] / c.iloc[-22] - 1.0),
        "ret_63d": float(c.iloc[-1] / c.iloc[-64] - 1.0),
        "ret_252d": float(c.iloc[-1] / c.iloc[-253] - 1.0),
        "vol_ann_1y": float(c.pct_change().iloc[-252:].std() * np.sqrt(252)),
        "trig_reclaim_200dma": reclaimed,
        "above_200dma": bool(last > float(sma200.iloc[-1])),
    }


# =============================================================================
# FUNDAMENTALS  (slow stage — cached, survivors only)
# =============================================================================

_ALIAS = {
    "revenue": ["Total Revenue", "Operating Revenue", "Revenue"],
    "cogs": ["Cost Of Revenue", "Cost of Revenue", "Reconciled Cost Of Revenue"],
    "gross_profit": ["Gross Profit"],
    "ebit": ["EBIT", "Operating Income", "Total Operating Income As Reported"],
    "ebitda": ["EBITDA", "Normalized EBITDA"],
    "net_income": ["Net Income", "Net Income Common Stockholders",
                   "Net Income From Continuing Operation Net Minority Interest"],
    "interest_exp": ["Interest Expense", "Interest Expense Non Operating",
                     "Net Interest Income"],
    "total_assets": ["Total Assets"],
    "cur_assets": ["Current Assets", "Total Current Assets"],
    "cur_liab": ["Current Liabilities", "Total Current Liabilities"],
    "total_debt": ["Total Debt"],
    "lt_debt": ["Long Term Debt", "Long Term Debt And Capital Lease Obligation"],
    "cash": ["Cash Cash Equivalents And Short Term Investments",
             "Cash And Cash Equivalents", "Cash Financial"],
    "equity": ["Stockholders Equity", "Common Stock Equity",
               "Total Equity Gross Minority Interest"],
    "goodwill_intang": ["Goodwill And Other Intangible Assets", "Goodwill"],
    "shares": ["Ordinary Shares Number", "Share Issued"],
    "inventory": ["Inventory"],
    "cfo": ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities"],
    "fcf": ["Free Cash Flow"],
    "capex": ["Capital Expenditure"],
    "buyback": ["Repurchase Of Capital Stock"],
}


def _row(df: pd.DataFrame | None, key: str, idx: int = 0) -> float:
    """Defensive statement lookup. idx=0 is most recent period."""
    if df is None or df.empty:
        return np.nan
    for label in _ALIAS.get(key, [key]):
        if label in df.index:
            try:
                val = df.loc[label].iloc[idx]
            except (IndexError, KeyError):
                continue
            if pd.notna(val):
                return float(val)
    return np.nan


def _series(df: pd.DataFrame | None, key: str) -> pd.Series:
    if df is None or df.empty:
        return pd.Series(dtype=float)
    for label in _ALIAS.get(key, [key]):
        if label in df.index:
            return df.loc[label].astype(float)
    return pd.Series(dtype=float)


def _cache_path(ticker: str, cfg: Config) -> str:
    os.makedirs(cfg.cache_dir, exist_ok=True)
    return os.path.join(cfg.cache_dir, f"{ticker}.json")


def _cache_fresh(path: str, ttl_days: int) -> bool:
    if not os.path.exists(path):
        return False
    age = time.time() - os.path.getmtime(path)
    return age < ttl_days * 86400


def fetch_fundamentals(ticker: str, cfg: Config = CFG) -> dict:
    """One ticker's full fundamental payload. Cached to disk."""
    path = _cache_path(ticker, cfg)
    if _cache_fresh(path, cfg.fundamentals_ttl_days):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass

    out = {"ticker": ticker, "fetched": datetime.now(timezone.utc).isoformat()}
    try:
        tk = yf.Ticker(ticker)
        info = tk.info or {}
        inc, bs, cf = tk.income_stmt, tk.balance_sheet, tk.cash_flow
        qinc, qbs = tk.quarterly_income_stmt, tk.quarterly_balance_sheet

        out.update(_static_fields(info))
        out.update(_annual_fields(inc, bs, cf))
        out.update(_quarterly_fields(qinc, qbs))
        out.update(_revision_fields(tk))
        out["ok"] = True
    except Exception as exc:  # noqa: BLE001
        out["ok"] = False
        out["error"] = f"{type(exc).__name__}: {exc}"

    try:
        with open(path, "w") as f:
            json.dump(out, f, default=str)
    except Exception:
        pass
    time.sleep(cfg.throttle_sec)
    return out


def _static_fields(info: dict) -> dict:
    return {
        "name": info.get("shortName") or info.get("longName"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "market_cap": info.get("marketCap"),
        "shares_out": info.get("sharesOutstanding"),
        "beta": info.get("beta"),
        "trailing_pe": info.get("trailingPE"),
        "forward_pe": info.get("forwardPE"),
        "pb": info.get("priceToBook"),
        "ev_info": info.get("enterpriseValue"),
        "held_insiders": info.get("heldPercentInsiders"),
        "held_inst": info.get("heldPercentInstitutions"),
        "short_pct_float": info.get("shortPercentOfFloat"),
        "div_yield": info.get("dividendYield"),
        "country": info.get("country"),
    }


def _annual_fields(inc, bs, cf) -> dict:
    d: dict = {}
    rev = _series(inc, "revenue").dropna()
    d["rev_hist"] = [float(x) for x in rev.values[:6]]
    d["rev_ttm"] = float(rev.iloc[0]) if len(rev) else np.nan

    ebit = _series(inc, "ebit").dropna()
    if len(rev) and len(ebit):
        n = min(len(rev), len(ebit), 5)
        margins = [ebit.iloc[i] / rev.iloc[i] for i in range(n) if rev.iloc[i] > 0]
        d["ebit_margin_hist"] = [float(m) for m in margins]
        d["ebit_margin_med5"] = float(np.median(margins)) if margins else np.nan
    else:
        d["ebit_margin_hist"], d["ebit_margin_med5"] = [], np.nan

    for k in ("gross_profit", "ebitda", "net_income", "interest_exp"):
        d[k] = _row(inc, k)
        d[k + "_p1"] = _row(inc, k, 1)
    for k in ("total_assets", "cur_assets", "cur_liab", "total_debt", "lt_debt",
              "cash", "equity", "goodwill_intang", "shares", "inventory"):
        d[k] = _row(bs, k)
        d[k + "_p1"] = _row(bs, k, 1)
    d["total_assets_p2"] = _row(bs, "total_assets", 2)
    d["shares_p3"] = _row(bs, "shares", 3)
    d["revenue_p1"] = _row(inc, "revenue", 1)

    for k in ("cfo", "fcf", "capex", "buyback"):
        d[k] = _row(cf, k)
    fcf_s = _series(cf, "fcf").dropna()
    d["fcf_avg3"] = float(fcf_s.iloc[:3].mean()) if len(fcf_s) else np.nan
    return d


def _quarterly_fields(qinc, qbs) -> dict:
    d: dict = {}
    qrev = _series(qinc, "revenue").dropna()
    qgp = _series(qinc, "gross_profit").dropna()
    qni = _series(qinc, "net_income").dropna()
    qebit = _series(qinc, "ebit").dropna()

    d["q_rev"] = [float(x) for x in qrev.values[:8]]
    d["q_ni"] = [float(x) for x in qni.values[:8]]

    gm = []
    for i in range(min(len(qrev), len(qgp), 6)):
        if qrev.iloc[i] > 0:
            gm.append(float(qgp.iloc[i] / qrev.iloc[i]))
    d["q_gross_margin"] = gm

    om = []
    for i in range(min(len(qrev), len(qebit), 6)):
        if qrev.iloc[i] > 0:
            om.append(float(qebit.iloc[i] / qrev.iloc[i]))
    d["q_op_margin"] = om

    d["rev_yoy_q0"] = (float(qrev.iloc[0] / qrev.iloc[4] - 1)
                       if len(qrev) >= 5 and qrev.iloc[4] else np.nan)
    d["rev_yoy_q1"] = (float(qrev.iloc[1] / qrev.iloc[5] - 1)
                       if len(qrev) >= 6 and qrev.iloc[5] else np.nan)

    d["q_shares"] = [float(x) for x in _series(qbs, "shares").dropna().values[:5]]
    d["q_inventory"] = [float(x) for x in _series(qbs, "inventory").dropna().values[:5]]
    d["q_cash"] = [float(x) for x in _series(qbs, "cash").dropna().values[:5]]
    d["q_debt"] = [float(x) for x in _series(qbs, "total_debt").dropna().values[:5]]
    return d


def _revision_fields(tk) -> dict:
    """EPS estimate revisions — the one genuinely daily fundamental signal."""
    d = {"rev_up_30d": np.nan, "rev_down_30d": np.nan,
         "eps_trend_30d_chg": np.nan, "eps_trend_90d_chg": np.nan}
    try:
        er = tk.eps_revisions
        if er is not None and not er.empty:
            row = er.loc["0y"] if "0y" in er.index else er.iloc[0]
            d["rev_up_30d"] = float(row.get("upLast30days", np.nan))
            d["rev_down_30d"] = float(row.get("downLast30days", np.nan))
    except Exception:
        pass
    try:
        et = tk.eps_trend
        if et is not None and not et.empty:
            row = et.loc["+1y"] if "+1y" in et.index else et.iloc[-1]
            cur = float(row.get("current", np.nan))
            d30 = float(row.get("30daysAgo", np.nan))
            d90 = float(row.get("90daysAgo", np.nan))
            if pd.notna(cur) and pd.notna(d30) and abs(d30) > 1e-6:
                d["eps_trend_30d_chg"] = cur / d30 - 1 if d30 > 0 else np.nan
            if pd.notna(cur) and pd.notna(d90) and abs(d90) > 1e-6:
                d["eps_trend_90d_chg"] = cur / d90 - 1 if d90 > 0 else np.nan
    except Exception:
        pass
    return d


def fetch_fundamentals_bulk(tickers: list[str], cfg: Config = CFG) -> dict[str, dict]:
    res: dict[str, dict] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=cfg.max_workers) as ex:
        futs = {ex.submit(fetch_fundamentals, t, cfg): t for t in tickers}
        for fut in as_completed(futs):
            t = futs[fut]
            try:
                res[t] = fut.result()
            except Exception as exc:  # noqa: BLE001
                res[t] = {"ticker": t, "ok": False, "error": str(exc)}
            done += 1
            if done % 25 == 0 or done == len(tickers):
                print(f"    fundamentals {done}/{len(tickers)}")
    return res
