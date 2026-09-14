"""스크리너 결과를 화면용 표(tables.json)로 만든다.

담는 것
  1. 신고가 순위표 — 점수, 게이트 통과수, 못 넘은 항목
  2. 딥밸류 순위표 — Composite, F-Score, 낙폭, 트리거 발생 여부
  3. 탈락 사유 집계 — 어느 조건이 다 걸러내고 있는가
  4. 깔때기 추이 — 날짜별 통과 개수 변화 (history.json 에 누적)

'왜 죽어 있는가' 한 줄 진단은 GEMINI_API_KEY 가 있을 때만 붙인다.
없으면 칸이 비고, 나머지는 그대로 나온다.
"""

import re
import sys

import requests

from common import DATA_DIR, ROOT, env, now_kst, read_json, write_json

SCREENER_DIR = ROOT / "screener"
TOP_ROWS = 20
HISTORY_DAYS = 60
GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
              "gemini-2.0-flash:generateContent")
DATE_RE = re.compile(r"(20\d{6})")


def newest(pattern):
    files = sorted(SCREENER_DIR.rglob(pattern))
    if not files:
        return []
    def key(p):
        m = DATE_RE.search(p.name)
        return m.group(1) if m else "0"
    latest = max(key(f) for f in files)
    return [f for f in files if key(f) == latest]


def sheet(path, name, header_row=1):
    import openpyxl
    try:
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    except Exception as e:
        print(f"  열기 실패 {path.name}: {e}", file=sys.stderr)
        return []
    if name not in wb.sheetnames:
        return []
    rows = list(wb[name].iter_rows(min_row=header_row, values_only=True))
    if len(rows) < 2:
        return []
    header = [str(h).strip() if h is not None else "" for h in rows[0]]
    return [dict(zip(header, r)) for r in rows[1:]
            if any(c is not None for c in r)]


def num(v, nd=3):
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return None


def pct(v):
    """비율을 퍼센트로. 부호는 자료마다 달라서 크기만 쓴다."""
    n = num(v, 4)
    return None if n is None else round(abs(n) * 100, 1)


def breakout_table():
    rows, funnel = [], {}
    for path in newest("*breakout*.xlsx"):
        universe = path.name.split("_")[0].upper()

        for d in sheet(path, "1_진단"):
            stage = str(d.get("단계") or "").replace("└", "").strip()
            n = d.get("종목수")
            if stage and isinstance(n, (int, float)):
                funnel.setdefault(universe, {})[stage] = int(n)

        for r in sheet(path, "3_전체신고가"):
            score = num(r.get("점수"))
            if not r.get("티커") or score is None:
                continue
            rows.append({
                "code": str(r["티커"]).strip(),
                "name": str(r.get("종목명") or "").strip(),
                "universe": universe,
                "sector": r.get("섹터") or "",
                "score": score,
                "gates": r.get("게이트통과수"),
                "missing": str(r.get("미달항목") or "").strip(),
                "missing_n": r.get("미달개수"),
                "drawdown": pct(r.get("최대낙폭")),
                "buyable": bool(r.get("매수가능")),
                "final": bool(r.get("최종후보")),
            })
    rows.sort(key=lambda x: x["score"], reverse=True)
    return rows[:TOP_ROWS], funnel


def deepvalue_table():
    rows, rejects = [], {}
    for path in newest("*deepvalue*.xlsx"):
        universe = path.stem.split("_")[1].upper() if "_" in path.stem else ""

        triggered = {str(r.get("Ticker")).strip()
                     for r in sheet(path, "01_Triggered", 3) if r.get("Ticker")}

        for r in sheet(path, "06_Reject_Reasons", 3):
            reason, n = r.get("reason"), r.get("n")
            if reason and isinstance(n, (int, float)):
                rejects[str(reason)] = rejects.get(str(reason), 0) + int(n)

        for r in sheet(path, "02_Candidates", 3):
            comp = num(r.get("Composite"))
            if not r.get("Ticker") or comp is None:
                continue
            code = str(r["Ticker"]).strip()
            rows.append({
                "code": code,
                "name": str(r.get("Company") or "").strip(),
                "universe": universe,
                "sector": r.get("Sector") or "",
                "score": comp,
                "value": num(r.get("Value")),
                "turn": num(r.get("Turn")),
                "fscore": r.get("F-Score"),
                "drawdown": pct(r.get("DD 3Y High")),
                "below200": r.get("Days <200DMA"),
                "earn_yld": pct(r.get("Norm Earn Yld")),
                "triggered": code in triggered,
                "why": "",
            })
    rows.sort(key=lambda x: x["score"], reverse=True)
    return rows[:TOP_ROWS], rejects


def add_diagnosis(rows, api_key, limit=8):
    """상위 종목에 '왜 죽어 있는가' 한 줄을 붙인다."""
    if not api_key or not rows:
        return rows
    target = rows[:limit]
    lines = "\n".join(
        f"{i+1}. {r['code']} {r['name']} / {r['sector']} / "
        f"3년고점대비 {r['drawdown']}% / 200일선 아래 {r['below200']}일 / "
        f"F-Score {r['fscore']} / 정상화이익수익률 {r['earn_yld']}%"
        for i, r in enumerate(target))
    prompt = (
        "아래는 딥밸류 스크리너가 뽑은 미국 주식이다. "
        "각 종목이 왜 이렇게 싸게 거래되는지 한 문장으로 써라. "
        "35자 이내, 단정적 투자 의견은 쓰지 말고 통상 알려진 업황·구조 요인만. "
        "확실하지 않으면 '확인 필요'라고 써라. "
        "설명 없이 '번호. 내용' 형식으로만 출력.\n\n" + lines)
    try:
        r = requests.post(GEMINI_URL, params={"key": api_key}, timeout=60,
                          json={"contents": [{"parts": [{"text": prompt}]}]})
        r.raise_for_status()
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        print(f"  진단 생략 ({e})", file=sys.stderr)
        return rows

    for line in text.splitlines():
        line = line.strip()
        if not line or "." not in line[:4]:
            continue
        head, _, body = line.partition(".")
        try:
            idx = int(head.strip()) - 1
        except ValueError:
            continue
        if 0 <= idx < len(target) and body.strip():
            target[idx]["why"] = body.strip()
    return rows


def update_history(funnel, dv_rows):
    """날짜별 통과 개수를 누적해 추이를 만든다."""
    path = DATA_DIR / "history.json"
    hist = read_json(path, default=[]) or []
    today = now_kst().strftime("%Y-%m-%d")

    total_highs = sum(v.get("유효 신고가", 0) for v in funnel.values())
    gate3 = sum(v.get("게이트 3개 이상", 0) for v in funnel.values())
    buyable = sum(v.get("어닝임박 배제 후 매수가능", 0) for v in funnel.values())

    entry = {
        "date": today,
        "highs": total_highs,
        "gate3": gate3,
        "buyable": buyable,
        "dv_candidates": len(dv_rows),
        "dv_triggered": sum(1 for r in dv_rows if r["triggered"]),
    }
    hist = [h for h in hist if h.get("date") != today] + [entry]
    hist = sorted(hist, key=lambda h: h["date"])[-HISTORY_DAYS:]
    write_json(path, hist)
    return hist


def main():
    if not SCREENER_DIR.exists():
        print("screener 폴더가 없습니다.", file=sys.stderr)
        return

    breakout, funnel = breakout_table()
    deepvalue, rejects = deepvalue_table()
    print(f"신고가 {len(breakout)}행, 딥밸류 {len(deepvalue)}행")

    deepvalue = add_diagnosis(deepvalue, env("GEMINI_API_KEY"))
    hist = update_history(funnel, deepvalue)

    write_json(DATA_DIR / "tables.json", {
        "updated_at": now_kst().isoformat(),
        "breakout": breakout,
        "deepvalue": deepvalue,
        "funnel": funnel,
        "rejects": sorted(rejects.items(), key=lambda kv: kv[1], reverse=True),
        "history": hist[-20:],
    })
    print(f"저장 완료 (추이 {len(hist)}일치)")


if __name__ == "__main__":
    main()
