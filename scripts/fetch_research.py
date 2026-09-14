"""종목별 증권사 리포트 링크를 모은다.

picks.json 의 종목마다
  1. 네이버 금융 리서치에서 해당 종목 리포트 목록을 긁고
  2. 실패하거나 비면 구글 뉴스에서 '종목명 목표주가' 로 대체한다.

리포트 본문(PDF)은 저작물이라 내려받거나 옮겨 적지 않는다.
제목·증권사·날짜와 원문 링크만 남긴다.
"""

import re
import sys
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote

from common import DATA_DIR, clean_text, dedupe, get, now_kst, read_json, write_json

NAVER_LIST = ("https://finance.naver.com/research/company_list.naver"
              "?searchType=itemCode&itemCode={code}")
NAVER_READ = "https://finance.naver.com/research/company_read.naver?nid={nid}"
ROW_RE = re.compile(
    r'company_read\.naver\?nid=(\d+)[^>]*>(.*?)</a>.*?'
    r'<td[^>]*>(.*?)</td>.*?<td[^>]*>(.*?)</td>.*?'
    r'<td class="date">(.*?)</td>',
    re.S)
MAX_PER_STOCK = 6


def from_naver_research(code):
    """네이버 금융 리서치 목록 파싱. 페이지 구조가 바뀌면 조용히 빈 값을 준다."""
    try:
        r = get(NAVER_LIST.format(code=code))
        r.encoding = "euc-kr"
        html = r.text
    except Exception as e:
        print(f"  리서치 조회 실패 ({code}): {e}", file=sys.stderr)
        return []

    out = []
    for nid, title, broker, _pdf, date in ROW_RE.findall(html):
        title = clean_text(title)
        if not title:
            continue
        out.append({
            "title": title,
            "broker": clean_text(broker),
            "date": clean_text(date).replace(".", "-").strip("-"),
            "url": NAVER_READ.format(nid=nid),
            "kind": "리포트",
        })
    return out[:MAX_PER_STOCK]


def from_google(name):
    url = ("https://news.google.com/rss/search?q="
           f"{quote(name + ' 목표주가')}&hl=ko&gl=KR&ceid=KR:ko")
    try:
        r = get(url)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception:
        return []

    out = []
    for item in root.findall("./channel/item")[:MAX_PER_STOCK]:
        title = clean_text(item.findtext("title"))
        source = clean_text(item.findtext("source")) or ""
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3].strip()
        out.append({
            "title": title,
            "broker": source,
            "date": "",
            "url": item.findtext("link"),
            "kind": "기사",
        })
    return out


def main():
    picks = (read_json(DATA_DIR / "picks.json") or {}).get("picks", [])
    if not picks:
        print("picks.json 이 비어 있습니다. make_picks.py 를 먼저 실행하세요.")
        return

    result, total = [], 0
    for p in picks:
        if p.get("market") == "kr" and p["code"].isdigit():
            rows = from_naver_research(p["code"])
        else:
            rows = []
        if not rows:
            rows = from_google(p["name"])
        rows = dedupe(rows)[:MAX_PER_STOCK]
        total += len(rows)
        print(f"리포트 {p['name']}: {len(rows)}건")
        result.append({"code": p["code"], "name": p["name"], "items": rows})
        time.sleep(0.4)

    write_json(DATA_DIR / "research.json",
               {"updated_at": now_kst().isoformat(), "total": total,
                "stocks": result})
    print(f"저장 완료 ({total}건)")


if __name__ == "__main__":
    main()
