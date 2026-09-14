"""스크리너 엑셀을 읽어 오늘의 종목(picks.json)을 만든다.

screener/ 폴더에 있는 엑셀을 읽는다.
  *breakout*.xlsx   시트 3_전체신고가   순위 기준: 점수
  *deepvalue*.xlsx  시트 01_Triggered  순위 기준: Composite

같은 종류가 여러 개면 파일명 날짜가 가장 최근인 것만 쓴다.
두 전략의 점수는 계산식이 달라 서로 비교할 수 없으므로,
합쳐서 줄 세우지 않고 전략별로 상위 몇 개씩 뽑아 합친다.
"""

import re
import sys
from pathlib import Path

from common import DATA_DIR, ROOT, now_kst, write_json

SCREENER_DIR = ROOT / "screener"
TOP_BREAKOUT = 4      # 신고가·물극필반에서 뽑을 개수
TOP_DEEPVALUE = 3     # 딥밸류에서 뽑을 개수
TABLE_ROWS = 30       # 화면 표에 남길 전체 순위 행 수

DATE_RE = re.compile(r"(20\d{6})")


def newest(pattern):
    """파일명 안의 날짜가 가장 큰 파일들을 고른다.

    스크리너가 screener/outputs/ 아래에 쓰므로 하위 폴더까지 훑는다.
    """
    files = sorted(SCREENER_DIR.rglob(pattern))
    if not files:
        return []
    def key(p):
        m = DATE_RE.search(p.name)
        return m.group(1) if m else "0"
    latest = max(key(f) for f in files)
    return [f for f in files if key(f) == latest]


def read_sheet(path, sheet_name, header_row):
    import openpyxl
    try:
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    except Exception as e:
        print(f"  열기 실패 {path.name}: {e}", file=sys.stderr)
        return []
    if sheet_name not in wb.sheetnames:
        print(f"  시트 없음 {path.name}: {sheet_name}", file=sys.stderr)
        return []
    ws = wb[sheet_name]
    rows = list(ws.iter_rows(min_row=header_row, values_only=True))
    if len(rows) < 2:
        return []
    header = [str(h).strip() if h is not None else "" for h in rows[0]]
    return [dict(zip(header, r)) for r in rows[1:] if any(c is not None for c in r)]


def num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def from_breakout():
    out = []
    for path in newest("*breakout*.xlsx"):
        universe = path.name.split("_")[0].upper()
        for row in read_sheet(path, "3_전체신고가", 1):
            score = num(row.get("점수"))
            if not row.get("티커") or score is None:
                continue
            gates = row.get("게이트통과수")
            note = f"G{gates}" if gates not in (None, "") else ""
            if row.get("최종후보") is True:
                note = (note + " 최종후보").strip()
            out.append({
                "code": str(row["티커"]).strip(),
                "name": str(row.get("종목명") or row["티커"]).strip(),
                "market": "us",
                "strategy": "신고가·물극필반",
                "universe": universe,
                "sector": row.get("섹터") or "",
                "score": round(score, 3),
                "note": note,
            })
    return out


def from_deepvalue():
    out = []
    for path in newest("*deepvalue*.xlsx"):
        universe = path.stem.split("_")[1].upper() if "_" in path.stem else ""
        for row in read_sheet(path, "01_Triggered", 3):
            score = num(row.get("Composite"))
            if not row.get("Ticker") or score is None:
                continue
            f = row.get("F-Score")
            out.append({
                "code": str(row["Ticker"]).strip(),
                "name": str(row.get("Company") or row["Ticker"]).strip(),
                "market": "us",
                "strategy": "딥밸류",
                "universe": universe,
                "sector": row.get("Sector") or "",
                "score": round(score, 3),
                "note": f"F{int(f)}" if num(f) is not None else "",
            })
    return out


def rank(rows):
    return sorted(rows, key=lambda r: r["score"], reverse=True)


def main():
    if not SCREENER_DIR.exists():
        print("screener 폴더가 없습니다. 엑셀을 올려주세요.", file=sys.stderr)
        write_json(DATA_DIR / "picks.json",
                   {"updated_at": now_kst().isoformat(), "picks": [], "table": []})
        return

    breakout = rank(from_breakout())
    deepvalue = rank(from_deepvalue())
    print(f"읽음: 신고가 {len(breakout)}개, 딥밸류 {len(deepvalue)}개")

    picks, seen = [], set()
    for row in breakout[:TOP_BREAKOUT] + deepvalue[:TOP_DEEPVALUE]:
        if row["code"] in seen:
            continue
        seen.add(row["code"])
        picks.append({**row, "source": row["strategy"]})

    table = []
    for group in (breakout, deepvalue):
        for i, row in enumerate(group[:TABLE_ROWS], 1):
            table.append({**row, "rank": i})

    write_json(DATA_DIR / "picks.json", {
        "updated_at": now_kst().isoformat(),
        "picks": picks,
        "table": table,
    })
    if picks:
        print("오늘의 종목: " + ", ".join(f"{p['name']}({p['score']})" for p in picks))
    else:
        print("뽑힌 종목이 없습니다. 엑셀 파일명과 시트 이름을 확인하세요.")


if __name__ == "__main__":
    main()
