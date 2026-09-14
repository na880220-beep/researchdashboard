#!/usr/bin/env python3
# =====================================================================
# US 52주 신고가 x 물극필반 스크리너 — 단일 파일판 (v2.0)
# =====================================================================
# 기존 Colab 3셀(universe / prices / scan)을 하나로 합친 로컬 실행판.
#
#   python us_breakout.py                  # 3개 지수 전부
#   python us_breakout.py -u SPX NDX       # 일부만
#   python us_breakout.py --full-refresh   # 캐시 무시하고 전체 재수집
#   python us_breakout.py --check          # 캐시 신선도만 확인하고 종료
#
# v1(3셀판) 대비 바뀐 점 — 게이트/트리거/점수 로직은 100% 그대로다.
#   1. [버그 수정] 가격 캐시 증분 갱신
#      v1: `todo = [t for t in tickers if t not in cache]`
#          → 캐시에 있으면 영원히 재다운로드 안 함. 첫 실행일 가격이 고정됨.
#      v2: 벤치마크(SPY)로 시장 최종 거래일을 먼저 확인하고, 캐시의 마지막
#          날짜가 그보다 이전인 종목만 골라 부족분 구간만 받아서 이어붙인다.
#   2. [분할/배당 방어] auto_adjust=True는 과거 가격까지 소급 조정되므로,
#      증분 구간을 겹치게(PAD일) 받아 겹치는 날의 종가가 어긋나면 그 종목만
#      전체 재다운로드한다.
#   3. [파일명] 실행일이 아니라 데이터 기준일로 저장한다.
#      v1은 8/27에 돌려도 8/4 데이터가 `..._20260827.xlsx`로 저장돼서
#      묵은 데이터인 걸 파일명만 봐선 알 수 없었다.
#   4. [신선도 가드] 기준일이 시장 최종 거래일보다 뒤처지면 엑셀 1_진단
#      맨 위와 콘솔에 경고를 띄운다. --strict면 아예 저장하지 않는다.
#   5. [유니버스 캐시] 위키/나스닥/iShares 스크래핑이 실패해도 최근 성공분을
#      재사용해 그날 스캔이 통째로 죽지 않게 한다.
# =====================================================================

from __future__ import annotations

import argparse
import datetime as dt
import io
import logging
import os
import sys
import time

import numpy as np
import pandas as pd
import requests

logging.getLogger("yfinance").setLevel(logging.CRITICAL)

# =====================================================================
# CONFIG — v1 셀1 CFG + 셀3 상향분을 합쳐 한 곳에 둠
# =====================================================================
CFG = dict(
    # ---- 데이터 ----
    HIST_YEARS      = 6,
    MIN_BARS        = 900,        # 약 3.6년. 미달 = 신규상장/SPAC 직후 → 판정불가
    CHUNK_SIZE      = 300,
    CACHE_PAD_DAYS  = 7,          # 증분 구간 겹침(분할 검증용)
    FULL_REDL_GAP   = 200,        # 캐시 공백이 이보다 크면 전체 재다운로드
    SPLIT_TOL       = 0.01,       # 겹침 구간 종가 괴리 1% 초과 → 분할 의심

    # ---- G1 신고가의 '나이' ----
    HIGH_WIN        = 252,
    MIN_HIGH_AGE    = 500,
    MAX_VS_5Y_PEAK  = 0.75,
    HIGH_TOL        = 0.001,

    # ---- G2 하락 이력 ----
    PEAK_WIN        = 1250,
    MIN_DRAWDOWN    = 0.40,

    # ---- G3 바닥 횡보 ----
    BOTTOM_BAND     = 0.25,
    MIN_BOTTOM_DAYS = 120,
    BASE_WIN        = 120,

    # ---- G4/G5 미장착 (컬럼 유지, False 고정) ----
    GATE_MIN        = 3,

    # ---- 2차 트리거 ----
    VOL_MULT        = 2.0,
    NEED_MA_STACK   = True,
    MA120_SLOPE_LAG = 20,
    RS_TOP          = 0.30,
    RS_WIN          = 120,
    MIN_FRESHNESS   = 0.35,

    # ---- 하드필터 (셀3 상향값 반영) ----
    MIN_MCAP_USD     = 300e6,
    MIN_TURNOVER_USD = 5e6,
    MAX_ZERO_VOL     = 3,
    MIN_UNIQ_CLOSE   = 15,
    MIN_PRICE_USD    = 3.0,

    EARNINGS_BLACKOUT_DAYS = 5,
    CHUNK_SLEEP_SEC = 1.0,
    MIN_ROWS_KEEP   = 30,
)

UNIVERSES = {
    "SPX": dict(name="S&P500",      bench="SPY"),
    "NDX": dict(name="Nasdaq100",   bench="QQQ"),
    "R2K": dict(name="Russell2000", bench="IWM"),
}
BENCH_TICKERS = sorted({v["bench"] for v in UNIVERSES.values()})
_HDRS = {"User-Agent": "Mozilla/5.0 (screener; personal research)"}

# 런타임에 채워지는 경로들
PATHS: dict[str, str] = {}


def _setup_paths(workdir: str):
    PATHS["work"] = os.path.abspath(workdir)
    PATHS["cache"] = os.path.join(PATHS["work"], "us_px_cache.pkl.gz")
    PATHS["univ"] = os.path.join(PATHS["work"], "universe_cache")
    PATHS["out"] = os.path.join(PATHS["work"], "outputs")
    PATHS["ratio_log"] = os.path.join(PATHS["work"], "breakout_ratio_log.csv")
    for k in ("work", "univ", "out"):
        os.makedirs(PATHS[k], exist_ok=True)


# =====================================================================
# 유니버스 빌더 (v1 셀1과 동일 + 실패시 최근 캐시 폴백)
# =====================================================================
def _clean_tickers(s: pd.Series) -> pd.Series:
    t = (s.astype(str).str.strip().str.upper()
          .str.replace(".", "-", regex=False)
          .str.replace("/", "-", regex=False))
    return t[t.str.fullmatch(r"[A-Z][A-Z0-9\-]{0,9}")]


def get_spx() -> pd.DataFrame:
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    html = requests.get(url, headers=_HDRS, timeout=30).text
    tables = pd.read_html(io.StringIO(html))
    df = next(t for t in tables if "Symbol" in t.columns and len(t) > 400)
    out = pd.DataFrame({"티커": _clean_tickers(df["Symbol"]),
                        "종목명": df["Security"].astype(str)})
    return out.dropna(subset=["티커"]).drop_duplicates("티커").reset_index(drop=True)


def get_ndx() -> pd.DataFrame:
    api_hdrs = dict(_HDRS)
    api_hdrs.update({
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9",
        "origin": "https://www.nasdaq.com",
        "referer": "https://www.nasdaq.com/market-activity/quotes/nasdaq-100-stocks",
    })
    try:
        r = requests.get("https://api.nasdaq.com/api/quote/list-type/nasdaq100",
                         headers=api_hdrs, timeout=20)
        r.raise_for_status()
        rows = r.json()["data"]["data"]["rows"]
        out = pd.DataFrame({
            "티커": _clean_tickers(pd.Series([x.get("symbol", "") for x in rows])),
            "종목명": [x.get("companyName", x.get("symbol", "")) for x in rows],
        })
        out = out.dropna(subset=["티커"]).drop_duplicates("티커")
        if len(out) >= 90:
            return out.reset_index(drop=True)
        print(f"      ⚠ 나스닥 API {len(out)}종목뿐 — SlickCharts 폴백")
    except Exception as e:
        print(f"      ⚠ 나스닥 API 실패({type(e).__name__}) — SlickCharts 폴백")

    sc_hdrs = dict(_HDRS)
    sc_hdrs.update({"accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                    "accept-language": "en-US,en;q=0.9"})
    html = requests.get("https://www.slickcharts.com/nasdaq100",
                        headers=sc_hdrs, timeout=30).text
    tables = pd.read_html(io.StringIO(html))
    df = next((t for t in tables
               if {"Symbol", "Company"} <= set(map(str, t.columns)) and 90 <= len(t) <= 115), None)
    if df is None:
        raise RuntimeError("SlickCharts 표 없음 (Cloudflare 차단 가능성)")
    out = pd.DataFrame({"티커": _clean_tickers(df["Symbol"]),
                        "종목명": df["Company"].astype(str)})
    return out.dropna(subset=["티커"]).drop_duplicates("티커").reset_index(drop=True)


def _parse_ishares_csv(text: str) -> pd.DataFrame:
    lines = text.splitlines()
    hdr = next((i for i, l in enumerate(lines) if l.startswith("Ticker,")), None)
    if hdr is None:
        raise RuntimeError("IWM CSV 헤더 없음 — iShares 포맷 변경 가능성")
    body = []
    for l in lines[hdr:]:
        if not l.strip():
            break
        body.append(l)
    df = pd.read_csv(io.StringIO("\n".join(body)))
    if "Asset Class" in df.columns:
        df = df[df["Asset Class"].astype(str).str.strip() == "Equity"]
    out = pd.DataFrame({"티커": _clean_tickers(df["Ticker"]),
                        "종목명": df.get("Name", df["Ticker"]).astype(str)})
    out = out.dropna(subset=["티커"]).drop_duplicates("티커")
    return out[~out["티커"].isin(["XTSLA", "MARGIN-CASH", "USD"])].reset_index(drop=True)


def get_r2k() -> pd.DataFrame:
    url = ("https://www.ishares.com/us/products/239710/"
           "ishares-russell-2000-etf/latest-holdings.csv")
    r = requests.get(url, headers=_HDRS, timeout=60)
    r.raise_for_status()
    return _parse_ishares_csv(r.text)


def build_universe(u: str) -> pd.DataFrame:
    """스크래핑 실패시 최근 성공 캐시로 폴백. 성공하면 오늘자로 캐시 저장."""
    path_today = os.path.join(PATHS["univ"], f"{u}_{dt.date.today():%Y%m%d}.csv")
    if os.path.exists(path_today):
        df = pd.read_csv(path_today)
        print(f"  {UNIVERSES[u]['name']:<12} {len(df):>5}종목  (오늘자 캐시)")
        return df
    try:
        df = {"SPX": get_spx, "NDX": get_ndx, "R2K": get_r2k}[u]()
        df.to_csv(path_today, index=False)
        print(f"  {UNIVERSES[u]['name']:<12} {len(df):>5}종목  "
              f"(벤치마크 {UNIVERSES[u]['bench']})")
        return df
    except Exception as e:
        olds = sorted(f for f in os.listdir(PATHS["univ"]) if f.startswith(f"{u}_"))
        if not olds:
            raise RuntimeError(f"{u} 유니버스 확보 실패, 캐시도 없음: {e}") from e
        latest = olds[-1]
        df = pd.read_csv(os.path.join(PATHS["univ"], latest))
        print(f"  {UNIVERSES[u]['name']:<12} {len(df):>5}종목  "
              f"⚠ 스크래핑 실패({type(e).__name__}) — 캐시 {latest} 사용")
        return df


# =====================================================================
# 가격 캐시 — 증분 갱신 (v1의 핵심 버그 수정 지점)
# =====================================================================
KEEP_COLS = ["Close", "Volume"]


def _slim(df):
    """캐시에 담을 최소 형태로 줄인다.

    스크리너가 실제로 쓰는 값은 종가와 거래량뿐이다.
    시가·고가·저가를 버리고 32비트로 낮추면 용량이 1/5 수준이 된다.
    """
    if df is None or df.empty:
        return df
    cols = [c for c in KEEP_COLS if c in df.columns]
    if not cols:
        return None
    out = df[cols].astype("float32")
    return out


def _load_cache() -> dict:
    p = PATHS["cache"]
    if os.path.exists(p):
        try:
            c = pd.read_pickle(p)
            c = {t: _slim(df) for t, df in c.items()}
            c = {t: df for t, df in c.items() if df is not None and not df.empty}
            print(f"      캐시 로드: {len(c)}종목")
            return c
        except Exception as e:
            print(f"      캐시 로드 실패({e}) — 새로 시작")
    return {}


def _save_cache(cache: dict):
    slim = {t: _slim(df) for t, df in cache.items()}
    slim = {t: df for t, df in slim.items() if df is not None and not df.empty}
    # 확장자가 .gz 라 pandas 가 알아서 압축한다
    pd.to_pickle(slim, PATHS["cache"], compression="infer")
    try:
        mb = os.path.getsize(PATHS["cache"]) / 1024 / 1024
        print(f"      캐시 파일 크기: {mb:.1f}MB")
        if mb > 45:
            print("      ⚠ 캐시가 45MB를 넘었습니다. GitHub 저장소 한도에 유의하세요.")
    except OSError:
        pass


def _last_date(df) -> dt.date:
    return pd.Timestamp(df.index[-1]).date()


def market_last_session() -> dt.date:
    """SPY를 매번 새로 받아 시장의 실제 최종 거래일을 확정한다.
    휴장일 캘린더를 따로 들 필요 없이 이게 제일 정확하다."""
    import yfinance as yf
    d = yf.download("SPY", period="15d", auto_adjust=True,
                    progress=False, threads=False)
    if d is None or d.empty:
        raise RuntimeError("SPY 조회 실패 — 네트워크/yfinance 상태 확인 필요")
    return pd.Timestamp(d.index[-1]).date()


def _extract(data, t: str):
    """yf.download 결과에서 티커 하나 뽑기. 단일티커 응답도 처리."""
    try:
        sub = data[t]
    except (KeyError, IndexError, TypeError):
        sub = data if "Close" in getattr(data, "columns", []) else None
    if sub is None:
        return None
    sub = sub.dropna(how="all")
    if sub.empty or "Close" not in sub.columns:
        return None
    need = [c for c in KEEP_COLS if c in sub.columns]
    return _slim(sub[need].dropna(subset=["Close"]))


def _download_chunks(tickers, start, label, verbose=True):
    """청크 단위 배치 다운로드 -> {티커: DataFrame}"""
    import yfinance as yf
    got = {}
    cs = CFG["CHUNK_SIZE"]
    n_chunks = (len(tickers) + cs - 1) // cs
    for ci in range(n_chunks):
        chunk = tickers[ci * cs:(ci + 1) * cs]
        data = None
        for attempt in range(3):
            try:
                data = yf.download(chunk, start=start, group_by="ticker",
                                   auto_adjust=True, threads=True,
                                   progress=False, timeout=30)
                break
            except Exception as e:
                if verbose:
                    print(f"      {label} 청크 {ci+1}/{n_chunks} 실패"
                          f"(시도 {attempt+1}/3): {type(e).__name__}")
                time.sleep(3 * (attempt + 1))
        if data is None or data.empty:
            if verbose:
                print(f"      {label} 청크 {ci+1}/{n_chunks} 전체 실패 — 건너뜀")
            continue
        for t in chunk:
            sub = _extract(data, t)
            if sub is not None and not sub.empty:
                got[t] = sub
        if verbose:
            print(f"      {label} 청크 {ci+1}/{n_chunks} 완료 (누적 {len(got)})")
        time.sleep(CFG["CHUNK_SLEEP_SEC"])
    return got


def _merge_incremental(old: pd.DataFrame, new: pd.DataFrame) -> tuple[pd.DataFrame, bool]:
    """겹침 구간 종가를 대조해 분할/조정 발생을 잡아낸다.
    반환: (병합결과, 재다운로드필요여부)"""
    ov = old.index.intersection(new.index)
    if len(ov) >= 2:
        a = old.loc[ov, "Close"].to_numpy(dtype=float)
        b = new.loc[ov, "Close"].to_numpy(dtype=float)
        with np.errstate(divide="ignore", invalid="ignore"):
            dev = np.nanmax(np.abs(b / a - 1))
        if np.isfinite(dev) and dev > CFG["SPLIT_TOL"]:
            return old, True     # 소급 조정 발생 → 전체 재다운로드 대상
    merged = pd.concat([old, new])
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    return merged, False


def fetch_prices(tickers, full_refresh=False, verbose=True):
    """캐시 증분 갱신 후 {티커: DataFrame} 반환."""
    years = CFG["HIST_YEARS"]
    full_start = (dt.date.today() - dt.timedelta(days=int(years * 365.25) + 30)).isoformat()

    cache = {} if full_refresh else _load_cache()
    mkt_last = market_last_session()
    print(f"      시장 최종 거래일: {mkt_last}")

    need_full, stale = [], []
    for t in tickers:
        if t not in cache:
            need_full.append(t)
            continue
        ld = _last_date(cache[t])
        if ld >= mkt_last:
            continue
        if (mkt_last - ld).days > CFG["FULL_REDL_GAP"]:
            need_full.append(t)
        else:
            stale.append((t, ld))

    fresh = len(tickers) - len(need_full) - len(stale)
    print(f"      신규/전체재수집 {len(need_full)} / 증분갱신 {len(stale)} / "
          f"최신 {fresh}")

    # --- 증분 갱신 ---
    if stale:
        oldest = min(ld for _, ld in stale)
        inc_start = (oldest - dt.timedelta(days=CFG["CACHE_PAD_DAYS"])).isoformat()
        print(f"      증분 구간: {inc_start} ~ (겹침 {CFG['CACHE_PAD_DAYS']}일)")
        got = _download_chunks([t for t, _ in stale], inc_start, "증분", verbose)
        redl = []
        for t, _ in stale:
            if t not in got:
                continue
            merged, need_redl = _merge_incremental(cache[t], got[t])
            if need_redl:
                redl.append(t)
            else:
                cache[t] = merged
        if redl:
            print(f"      분할/조정 감지 {len(redl)}종목 — 전체 재다운로드 대상에 추가")
            need_full.extend(redl)
        _save_cache(cache)

    # --- 신규 / 전체 재수집 ---
    if need_full:
        got = _download_chunks(need_full, full_start, "전체", verbose)
        for t, sub in got.items():
            if len(sub) >= CFG["MIN_ROWS_KEEP"]:
                cache[t] = sub
        _save_cache(cache)

    print(f"      캐시 저장 -> {PATHS['cache']} (총 {len(cache)}종목)")

    n_stale_left = sum(1 for t in tickers
                       if t in cache and _last_date(cache[t]) < mkt_last)
    if n_stale_left:
        print(f"      ⚠ 갱신 실패로 여전히 뒤처진 종목 {n_stale_left}개 "
              f"(야후 레이트리밋일 수 있음 — 잠시 후 재실행 권장)")
    return {t: cache[t] for t in tickers if t in cache}, mkt_last


def cache_status():
    """--check 전용: 캐시 신선도 요약."""
    cache = _load_cache()
    if not cache:
        print("캐시 없음.")
        return
    dates = pd.Series([_last_date(d) for d in cache.values()])
    vc = dates.value_counts().sort_index(ascending=False)
    print(f"\n캐시 {len(cache)}종목의 마지막 데이터 날짜 분포 (상위 8개)")
    for d, n in vc.head(8).items():
        print(f"  {d}   {n:>5}종목")
    try:
        m = market_last_session()
        top = vc.index[0]
        gap = (m - top).days
        print(f"\n시장 최종 거래일: {m}  /  캐시 최다 날짜: {top}  (차이 {gap}일)")
        print("→ 정상" if gap <= 1 else "→ ⚠ 캐시가 뒤처져 있음. 그냥 실행하면 자동 증분 갱신됨.")
    except Exception as e:
        print(f"\n시장 최종 거래일 확인 실패: {e}")


# =====================================================================
# [C] 가격 피처 — v1 셀3과 완전히 동일
# =====================================================================
def high_age(close: pd.Series, win=None, tol=None):
    win = win or CFG["HIGH_WIN"]
    tol = CFG["HIGH_TOL"] if tol is None else tol
    n = len(close)
    if n < win + 5:
        return False, np.nan, np.nan
    arr = close.to_numpy(dtype=float)
    today = arr[-1]
    if today < arr[-win:].max() * (1 - tol):
        return False, np.nan, np.nan
    prior = arr[:-1]
    above = np.flatnonzero(prior >= today * (1 - tol))
    age = int((n - 1) - above[-1]) if above.size else int(n)
    below = np.flatnonzero(prior <= today * 0.93)
    run_start = int(below[-1]) if below.size else max(0, n - CFG["BASE_WIN"] - 1)
    return True, age, run_start


def price_features(df: pd.DataFrame):
    c, v = df["Close"], df["Volume"]
    n = len(c)
    out = {}
    today = float(c.iloc[-1])

    w = min(CFG["PEAK_WIN"], n)
    arr = c.iloc[-w:].to_numpy(dtype=float)
    pk_pos = int(np.argmax(arr))
    peak = float(arr[pk_pos])
    trough = float(arr[pk_pos:].min())

    out["최대낙폭"] = 1 - trough / peak if peak > 0 else np.nan
    out["5년고점대비"] = today / peak if peak > 0 else np.nan
    out["바닥체류일수"] = int((arr <= trough * (1 + CFG["BOTTOM_BAND"])).sum())

    win52 = min(CFG["HIGH_WIN"], n)
    out["52주최고가"] = round(float(c.iloc[-win52:].max()), 2)
    out["5년고점"] = round(peak, 2)

    def _ret(k):
        return today / float(c.iloc[-k]) - 1 if n > k else np.nan
    r60, r500 = _ret(60), _ret(500)
    out["수익률60D"] = r60
    out["수익률500D"] = r500
    out["돌파신선도"] = (r60 / r500) if (pd.notna(r500) and r500 > 0.10) else np.nan

    e = n - 1 - 20
    if e - CFG["BASE_WIN"] >= 0:
        b = c.iloc[e - CFG["BASE_WIN"]:e]
        bmax, bmin = float(b.max()), float(b.min())
        out["베이스깊이"] = (bmax - bmin) / bmax if bmax > 0 else np.nan
    else:
        out["베이스깊이"] = np.nan

    if n >= 22:
        bv = float(v.iloc[-21:-1].mean())
        out["거래량배수"] = float(v.iloc[-1]) / bv if bv > 0 else np.nan
    else:
        out["거래량배수"] = np.nan

    ma = {pp: c.rolling(pp).mean() for pp in (5, 20, 60, 120)}
    try:
        m5, m20, m60, m120 = (float(ma[pp].iloc[-1]) for pp in (5, 20, 60, 120))
        m120_prev = float(ma[120].iloc[-1 - CFG["MA120_SLOPE_LAG"]])
        out["정배열"] = bool(m5 > m20 > m60 > m120)
        out["120일선상향"] = bool(m120 > m120_prev)
        out["이격도60"] = today / m60 - 1 if m60 > 0 else np.nan
    except Exception:
        out["정배열"] = False
        out["120일선상향"] = False
        out["이격도60"] = np.nan

    out["_수익률6M"] = _ret(CFG["RS_WIN"])
    out["종가"] = today
    out["기준일"] = pd.Timestamp(c.index[-1]).strftime("%Y-%m-%d")
    return out


def is_tradable(df: pd.DataFrame):
    c, v = df["Close"], df["Volume"]
    if len(c) < 60:
        return False, "데이터부족"
    if float(c.iloc[-1]) < CFG["MIN_PRICE_USD"]:
        return False, f"저가주(${float(c.iloc[-1]):.2f})"
    if int((v.iloc[-60:].fillna(0) <= 0).sum()) > CFG["MAX_ZERO_VOL"]:
        return False, "거래정지(거래량0)"
    if int(c.iloc[-60:].round(4).nunique()) < CFG["MIN_UNIQ_CLOSE"]:
        return False, "가격정지(종가일직선)"
    turnover = float((c.iloc[-20:] * v.iloc[-20:]).mean())
    if not np.isfinite(turnover) or turnover < CFG["MIN_TURNOVER_USD"]:
        return False, f"저유동성(${turnover/1e6:.1f}M)"
    return True, ""


def scan_prices(px: dict, bench_close, verbose=True):
    rows, dropped = [], []
    for t, df in px.items():
        if df is None or len(df) < CFG["MIN_BARS"]:
            continue
        c = df["Close"].dropna()
        if len(c) < CFG["MIN_BARS"]:
            continue
        ok, why = is_tradable(df.loc[c.index])
        if not ok:
            if high_age(c)[0]:
                dropped.append((t, why))
            continue
        is_h, age, _ = high_age(c)
        if not is_h:
            continue
        feat = price_features(df.loc[c.index])
        feat.update({"티커": t, "신고가나이": age})
        rows.append(feat)

    out = pd.DataFrame(rows)
    if out.empty:
        out.attrs["dropped"] = dropped
        return out

    if bench_close is not None and len(bench_close) > CFG["RS_WIN"]:
        idx_ret = float(bench_close.iloc[-1] / bench_close.iloc[-CFG["RS_WIN"]] - 1)
    else:
        idx_ret = 0.0
    out["지수대비초과"] = out["_수익률6M"] - idx_ret
    out["RS백분위"] = out["지수대비초과"].rank(pct=True)
    if verbose:
        print(f"      신고가 종목 {len(out)}개 / 벤치마크 6M {idx_ret:+.1%}")
        if dropped:
            print(f"      유동성/페니주 제외 {len(dropped)}종목: "
                  + ", ".join(f"{c}({w})" for c, w in dropped[:8])
                  + (" ..." if len(dropped) > 8 else ""))
    out.attrs["dropped"] = dropped
    return out


# =====================================================================
# 후보 부가정보 (시총 / 국가 / 섹터 / 실적일)
# =====================================================================
def get_candidate_extra(tickers, verbose=True):
    import yfinance as yf
    out = {}
    for t in tickers:
        row = {"marketCap": np.nan, "country": None, "sector": None, "industry": None}
        try:
            info = yf.Ticker(t).info
            row["marketCap"] = float(info.get("marketCap")) if info.get("marketCap") else np.nan
            row["country"] = info.get("country")
            row["sector"] = info.get("sector")
            row["industry"] = info.get("industry")
        except Exception:
            pass
        out[t] = row
        time.sleep(0.15)
    if verbose:
        ok = sum(1 for v in out.values() if np.isfinite(v["marketCap"]))
        print(f"      종목정보 {ok}/{len(tickers)}종목 확보")
    return out


def get_next_earnings_info(tickers, verbose=True):
    import yfinance as yf
    blackout = CFG["EARNINGS_BLACKOUT_DAYS"]
    today = pd.Timestamp(dt.date.today())
    out = {}
    for t in tickers:
        next_dt, tdays, soon = None, None, False
        try:
            tk = yf.Ticker(t)
            cand = None
            cal = tk.calendar
            if isinstance(cal, dict) and cal.get("Earnings Date"):
                fut = [pd.Timestamp(d) for d in cal["Earnings Date"] if d is not None]
                fut = [d for d in fut if d >= today]
                if fut:
                    cand = min(fut)
            if cand is None:
                edf = tk.get_earnings_dates(limit=8)
                if edf is not None and not edf.empty:
                    idx = [pd.Timestamp(d).tz_localize(None) for d in edf.index]
                    fut = [d for d in idx if d >= today]
                    if fut:
                        cand = min(fut)
            if cand is not None:
                next_dt = cand.date()
                tdays = int(len(pd.bdate_range(today, cand)))
                soon = tdays <= blackout
        except Exception:
            pass
        out[t] = {"next_earnings": next_dt, "trading_days": tdays, "어닝임박": soon}
        time.sleep(0.15)
    if verbose:
        n = sum(1 for v in out.values() if v["어닝임박"])
        print(f"      실적발표일 조회 완료 — {blackout}거래일 내 임박 {n}/{len(tickers)}")
    return out


# =====================================================================
# [E] 게이트 통합 + 랭킹 — v1과 완전히 동일
# =====================================================================
def apply_gates(df: pd.DataFrame):
    d = df.copy()
    d["G1_신고가나이"] = (d["신고가나이"].fillna(0) >= CFG["MIN_HIGH_AGE"]) & \
                         (d["5년고점대비"] <= CFG["MAX_VS_5Y_PEAK"])
    d["G2_하락이력"] = d["최대낙폭"] >= CFG["MIN_DRAWDOWN"]
    d["G3_바닥횡보"] = d["바닥체류일수"] >= CFG["MIN_BOTTOM_DAYS"]
    for c in ("G4_실적변곡", "G5_밸류하단"):
        if c not in d:
            d[c] = False
    d["G6_커버리지"] = False

    gcols = ["G1_신고가나이", "G2_하락이력", "G3_바닥횡보",
             "G4_실적변곡", "G5_밸류하단", "G6_커버리지"]
    d[gcols] = d[gcols].fillna(False).astype(bool)
    d["게이트통과수"] = d[gcols].sum(axis=1)

    d["T_거래량"] = d["거래량배수"] >= CFG["VOL_MULT"]
    d["T_정배열"] = (~CFG["NEED_MA_STACK"]) | (d["정배열"] & d["120일선상향"])
    d["T_RS"] = d["RS백분위"] >= (1 - CFG["RS_TOP"])
    d["T_신선도"] = d["돌파신선도"] >= CFG["MIN_FRESHNESS"]
    d["트리거통과"] = d["T_거래량"] & d["T_정배열"] & d["T_RS"] & d["T_신선도"]
    d["최종후보"] = (d["게이트통과수"] >= CFG["GATE_MIN"]) & d["트리거통과"]

    labels = [("G1_신고가나이", "G1신고가나이"), ("G2_하락이력", "G2하락이력"),
              ("G3_바닥횡보", "G3바닥횡보"),
              ("T_거래량", "T거래량"), ("T_정배열", "T정배열"),
              ("T_RS", "TRS"), ("T_신선도", "T신선도")]
    lcols = [c for c, _ in labels]
    d["미달항목"] = [", ".join(lbl for col, lbl in labels if not bool(r[col]))
                    for _, r in d[lcols].iterrows()]
    d["미달개수"] = d[lcols].apply(lambda r: int((~r.astype(bool)).sum()), axis=1)

    def nrm(s):
        s = pd.to_numeric(s, errors="coerce")
        lo, hi = s.quantile(0.05), s.quantile(0.95)
        return ((s - lo) / (hi - lo)).clip(0, 1).fillna(0) if hi > lo else s * 0

    d["점수"] = (
        0.30 * nrm(d["신고가나이"].replace(np.inf, 2000)) +
        0.25 * nrm(d["최대낙폭"]) +
        0.20 * nrm(d["돌파신선도"].clip(upper=3)) +
        0.15 * nrm(d["RS백분위"]) +
        0.10 * nrm(d["거래량배수"].clip(upper=10))
    ).round(3)
    return d


def log_breakout_ratio(u, date_str, denom, valid):
    ratio = (valid / denom) if denom else float("nan")
    row = dict(date=date_str, universe=u, n_universe=denom,
               n_valid_high=valid, ratio=round(ratio, 6))
    p = PATHS["ratio_log"]
    log = pd.read_csv(p) if os.path.exists(p) else pd.DataFrame(columns=list(row))
    log = log[~((log["date"] == row["date"]) & (log["universe"] == row["universe"]))]
    log = pd.concat([log, pd.DataFrame([row])], ignore_index=True)
    log.sort_values(["date", "universe"]).reset_index(drop=True).to_csv(p, index=False)


# =====================================================================
# 유니버스 하나 실행
# =====================================================================
def run_universe(u, univ_df, px, bench, mkt_last, strict=False, verbose=True):
    info = UNIVERSES[u]
    name, bench_ticker = info["name"], info["bench"]
    print(f"\n{'='*60}\n{name} ({u}) 스캔 — 벤치마크 {bench_ticker}\n{'='*60}")

    tickers = univ_df["티커"].tolist()
    stats = {"n_all": len(tickers)}

    print("[1/6] 가격 데이터 확보 현황")
    px_u = {t: px[t] for t in tickers if t in px}
    stats["n_px"] = len(px_u)
    long_px = {t: d for t, d in px_u.items() if len(d) >= CFG["MIN_BARS"]}
    stats["n_long"] = len(long_px)
    print(f"      가격 확보 {stats['n_px']}/{stats['n_all']} / "
          f"히스토리 {CFG['MIN_BARS']}일 이상 {stats['n_long']}")

    bench_close = bench.get(bench_ticker)
    if bench_close is None:
        print(f"      ⚠ 벤치마크 {bench_ticker} 없음 — RS 초과수익 0 처리")

    print("[2/6] 52주 신고가 스캔")
    scan = scan_prices(long_px, bench_close, verbose=verbose)
    dropped = scan.attrs.get("dropped", [])
    n_raw = len(scan) + len(dropped)

    # 기준일 = 데이터의 실제 마지막 거래일. 파일명도 여기서 만든다.
    asof = (str(scan["기준일"].mode().iloc[0]) if not scan.empty
            else str(mkt_last))
    lag = (mkt_last - dt.date.fromisoformat(asof)).days
    warn = None
    if lag > 0:
        warn = (f"⚠ 데이터 기준일 {asof} 이 시장 최종 거래일 {mkt_last} 보다 "
                f"{lag}일 뒤처짐 — 가격 갱신 실패 가능성")
        print(f"      {warn}")
        if strict:
            print("      --strict 지정됨 — 저장하지 않고 종료")
            return pd.DataFrame()

    op = os.path.join(PATHS["out"], f"{u}_breakout_{asof.replace('-','')}.xlsx")

    def _funnel_head(extra=()):
        base = [(f"{name} 전체", stats["n_all"]),
                ("가격 데이터 확보", stats["n_px"]),
                (f"히스토리 {CFG['MIN_BARS']}일 이상", stats["n_long"]),
                ("오늘 52주 신고가", n_raw)]
        return ([("데이터 기준일", asof), ("시장 최종 거래일", str(mkt_last))]
                + ([("⚠ 신선도 경고", warn)] if warn else []) + base + list(extra))

    if scan.empty:
        print("      오늘 52주 신고가 0개.")
        fdf = pd.DataFrame(_funnel_head(), columns=["단계", "종목수"])
        with pd.ExcelWriter(op, engine="openpyxl") as w:
            fdf.to_excel(w, sheet_name="1_진단", index=False)
        print(f"저장 -> {op} (진단 시트만)")
        log_breakout_ratio(u, asof, stats["n_long"], 0)
        return pd.DataFrame()

    print("[3/6] 종목정보 + 시총 필터 (신고가 후보에만 적용)")
    n_before = len(scan)
    extra = get_candidate_extra(scan["티커"].tolist(), verbose=verbose)
    for col, key in [("시가총액", "marketCap"), ("국가", "country"),
                     ("섹터", "sector"), ("산업", "industry")]:
        scan[col] = scan["티커"].map(lambda t, k=key: extra[t][k])
    scan = scan[scan["시가총액"].fillna(0) >= CFG["MIN_MCAP_USD"]].copy()
    n_scan = len(scan)
    print(f"      {n_before} -> {n_scan}종목 (시총 ${CFG['MIN_MCAP_USD']/1e6:.0f}M 이상)")

    if scan.empty:
        fdf = pd.DataFrame(_funnel_head([
            ("  └ 거래정지/저유동성 제외", -len(dropped)),
            ("  └ 유효 신고가", n_before),
            (f"  └ 시총 ${CFG['MIN_MCAP_USD']/1e6:.0f}M 통과", 0)]),
            columns=["단계", "종목수"])
        with pd.ExcelWriter(op, engine="openpyxl") as w:
            fdf.to_excel(w, sheet_name="1_진단", index=False)
        print(f"저장 -> {op} (진단 시트만)")
        log_breakout_ratio(u, asof, stats["n_long"], 0)
        return pd.DataFrame()

    print("[4/6] 게이트 적용 (가격 게이트만 — G4/G5는 수동)")
    res = apply_gates(scan)
    res = res.merge(univ_df, on="티커", how="left", suffixes=("", "_univ"))
    if "종목명_univ" in res.columns:
        res["종목명"] = res["종목명"].fillna(res["종목명_univ"])
        res = res.drop(columns=["종목명_univ"])

    print("[5/6] 실적발표 임박 + 중국ADR·바이오 힌트 (근접후보만)")
    targets = res.loc[res["미달개수"] <= 2, "티커"].tolist()
    earn = get_next_earnings_info(targets, verbose=verbose) if targets else {}
    res["다음실적일"] = res["티커"].map(lambda t: earn.get(t, {}).get("next_earnings"))
    res["어닝임박"] = res["티커"].map(lambda t: bool(earn.get(t, {}).get("어닝임박", False)))
    res["중국ADR_의심"] = res.get("국가", pd.Series(dtype=object)).astype(str).str.contains(
        "China", case=False, na=False)
    res["바이오검토필요"] = (
        res.get("섹터", pd.Series(dtype=object)).astype(str).str.contains("Health", case=False, na=False) |
        res.get("산업", pd.Series(dtype=object)).astype(str).str.contains("Biotechnology|Pharma", case=False, na=False))
    res["매수가능"] = res["최종후보"] & (~res["어닝임박"])

    cols = ["티커", "종목명", "매수가능", "어닝임박", "다음실적일",
            "중국ADR_의심", "바이오검토필요", "국가", "섹터", "산업",
            "시가총액", "종가", "52주최고가", "5년고점", "5년고점대비", "점수", "최종후보",
            "미달개수", "미달항목", "게이트통과수",
            "G1_신고가나이", "G2_하락이력", "G3_바닥횡보",
            "G4_실적변곡", "G5_밸류하단", "G6_커버리지",
            "신고가나이", "최대낙폭", "바닥체류일수",
            "돌파신선도", "수익률60D", "수익률500D", "베이스깊이",
            "거래량배수", "정배열", "120일선상향", "이격도60",
            "RS백분위", "지수대비초과",
            "T_거래량", "T_정배열", "T_RS", "T_신선도", "트리거통과", "기준일"]
    res = res[[c for c in cols if c in res.columns]].sort_values(
        ["미달개수", "점수"], ascending=[True, False])

    print("[6/6] 깔때기 진단 + 엑셀 출력")
    funnel = _funnel_head([
        ("  └ 거래정지/저유동성 제외", -len(dropped)),
        ("  └ 유효 신고가", n_before),
        (f"  └ 시총 ${CFG['MIN_MCAP_USD']/1e6:.0f}M/거래대금 "
         f"${CFG['MIN_TURNOVER_USD']/1e6:.0f}M 통과", n_scan)])
    for col, lbl in [("G1_신고가나이", "G1 신고가나이"), ("G2_하락이력", "G2 하락이력"),
                     ("G3_바닥횡보", "G3 바닥횡보"), ("T_거래량", "T 거래량"),
                     ("T_정배열", "T 정배열"), ("T_RS", "T 상대강도"),
                     ("T_신선도", "T 돌파신선도")]:
        if col in res:
            funnel.append((f"  └ {lbl} 단독 통과", int(res[col].sum())))
    funnel += [
        (f"게이트 {CFG['GATE_MIN']}개 이상", int((res["게이트통과수"] >= CFG["GATE_MIN"]).sum())),
        ("트리거 전부 통과", int(res["트리거통과"].sum())),
        ("최종후보 (가격+트리거만)", int(res["최종후보"].sum())),
        ("  └ 어닝임박 배제 후 매수가능", int(res["매수가능"].sum())),
    ]
    fdf = pd.DataFrame(funnel, columns=["단계", "종목수"])

    print("\n" + "=" * 46)
    print(f"  {name} 깔때기 진단")
    print("=" * 46)
    for k, v in funnel:
        print(f"  {str(k):<32} {str(v):>6}")
    print("=" * 46)

    near = res[res["미달개수"] <= 2]
    base = res[res["최종후보"]].copy()
    checklist = pd.DataFrame({
        "티커": base["티커"], "종목명": base["종목명"],
        "매수가능(자동판정)": base["매수가능"], "어닝임박(자동)": base["어닝임박"],
        "다음실적일(자동)": base["다음실적일"],
        "중국ADR_의심(자동힌트)": base["중국ADR_의심"],
        "바이오검토필요(자동힌트)": base["바이오검토필요"],
        "G4_실적변곡_수동확인": "", "G5_밸류밴드하위50%_수동확인": "",
        "희석오버행_수동확인": "", "바이너리이벤트_수동확인": "",
        "상장폐지감사의견_수동확인": "", "최종매수판단": "",
    })

    meta = pd.DataFrame([{"항목": k, "값": str(v)} for k, v in CFG.items()] + [
        {"항목": "유니버스", "값": name},
        {"항목": "RS벤치마크", "값": bench_ticker},
        {"항목": "데이터기준일", "값": asof},
        {"항목": "시장최종거래일", "값": str(mkt_last)},
        {"항목": "신선도", "값": warn or f"정상 (기준일 = 시장 최종 거래일 {mkt_last})"},
        {"항목": "생성시각", "값": dt.datetime.now().isoformat(timespec="seconds")},
        {"항목": "스크립트버전", "값": "v2.0 single-file (증분캐시)"},
        {"항목": "가격출처", "값": "yfinance (Yahoo Finance)"},
        {"항목": "재무출처", "값": "미장착 — G4/G5 항상 False. 6_매수체크리스트에서 수동 확인"},
        {"항목": "매수가능 정의", "값": "최종후보 AND 어닝임박 아님. G4/G5·희석오버행·"
                                  "바이너리이벤트·상장폐지는 미반영"},
    ])

    with pd.ExcelWriter(op, engine="openpyxl") as w:
        fdf.to_excel(w, sheet_name="1_진단", index=False)
        near.to_excel(w, sheet_name="2_근접후보", index=False)
        res.to_excel(w, sheet_name="3_전체신고가", index=False)
        res[res["최종후보"]].to_excel(w, sheet_name="4_최종후보", index=False)
        meta.to_excel(w, sheet_name="5_Meta", index=False)
        checklist.to_excel(w, sheet_name="6_매수체크리스트", index=False)

    print(f"\n저장 -> {op}")
    print(f"  신고가 {n_scan} / 근접후보 {len(near)} / "
          f"최종후보 {int(res['최종후보'].sum())} / 매수가능 {int(res['매수가능'].sum())}")
    log_breakout_ratio(u, asof, stats["n_long"], n_scan)
    return res


# =====================================================================
# main
# =====================================================================
def main():
    ap = argparse.ArgumentParser(description="US 52주 신고가 x 물극필반 스크리너")
    ap.add_argument("-u", "--universes", nargs="+", default=["SPX", "NDX", "R2K"],
                    choices=["SPX", "NDX", "R2K"])
    ap.add_argument("-w", "--workdir", default=os.path.expanduser("~/us_breakout"))
    ap.add_argument("--full-refresh", action="store_true", help="캐시 무시하고 전체 재수집")
    ap.add_argument("--check", action="store_true", help="캐시 신선도만 확인하고 종료")
    ap.add_argument("--strict", action="store_true",
                    help="데이터가 시장 최종 거래일보다 뒤처지면 저장하지 않음")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args()

    _setup_paths(args.workdir)
    print(f"작업 폴더: {PATHS['work']}")

    if args.check:
        cache_status()
        return 0

    verbose = not args.quiet
    t0 = time.time()

    print("\n[유니버스 구축]")
    univ = {}
    for u in args.universes:
        try:
            univ[u] = build_universe(u)
        except Exception as e:
            print(f"  ⚠ {u} 실패: {e}")
    if not univ:
        print("유니버스를 하나도 확보하지 못함 — 중단")
        return 1

    print("\n[가격 수집]")
    all_t = sorted(set().union(*[set(d["티커"]) for d in univ.values()]) | set(BENCH_TICKERS))
    print(f"      대상 {len(all_t)}종목 (중복 제거 + 벤치마크)")
    px, mkt_last = fetch_prices(all_t, full_refresh=args.full_refresh, verbose=verbose)
    bench = {b: px[b]["Close"] for b in BENCH_TICKERS if b in px}
    print(f"      가격 확보 {len(px)}/{len(all_t)}")

    for u in args.universes:
        if u in univ:
            try:
                run_universe(u, univ[u], px, bench, mkt_last,
                             strict=args.strict, verbose=verbose)
            except Exception as e:
                print(f"\n⚠ {u} 스캔 실패: {type(e).__name__}: {e}")

    print(f"\n완료 ({time.time()-t0:.0f}초). 출력 폴더: {PATHS['out']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
