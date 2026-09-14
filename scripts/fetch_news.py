"""뉴스 수집기.

두 갈래로 모은다.
  1. 종목 뉴스 — picks.json 에 오늘 올라온 종목별로
  2. 섹터 뉴스 — sectors.yaml 의 고정 관심 섹터별로

수집원 우선순위
  네이버 뉴스 API (키가 있으면) → 구글 뉴스 RSS (폴백)

요약
  GEMINI_API_KEY 가 있으면 한 줄 요약, 없으면 본문 앞부분.
  본문이 제목을 되풀이할 뿐이면(구글 RSS에서 흔함) 요약을 비운다.
"""

import sys
import time
import xml.etree.ElementTree as ET
from datetime import timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests

from common import (DATA_DIR, KST, clean_text, dedupe, echoes_title, env, get,
                    load_config, now_kst, read_json, write_json)

LOOKBACK_HOURS = 48
PER_QUERY = 30          # 한 검색어에서 가져올 최대 건수
DEFAULT_MAX = 20        # 한 묶음에 남길 최대 건수 (화면은 5개만 먼저 보여준다)
GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
              "gemini-2.0-flash:generateContent")


def parse_date(value):
    try:
        return parsedate_to_datetime(value).astimezone(KST)
    except Exception:
        return None


def source_from_url(url):
    if not url:
        return ""
    return url.split("//")[-1].split("/")[0].replace("www.", "")


def from_naver(query, creds):
    url = "https://openapi.naver.com/v1/search/news.json"
    try:
        r = get(url,
                params={"query": query, "display": PER_QUERY, "sort": "date"},
                headers={"X-Naver-Client-Id": creds["naver_id"],
                         "X-Naver-Client-Secret": creds["naver_secret"]})
        r.raise_for_status()
        rows = r.json().get("items", [])
    except Exception as e:
        print(f"  네이버 실패 ({query}): {e}", file=sys.stderr)
        return []

    out = []
    for row in rows:
        link = row.get("originallink") or row.get("link")
        out.append({
            "title": clean_text(row.get("title")),
            "body": clean_text(row.get("description")),
            "url": link,
            "source": source_from_url(link),
            "published": parse_date(row.get("pubDate")),
        })
    return out


def from_google(query, lang="ko"):
    """lang='ko' 면 한국 기사, 'en' 이면 영문 기사를 가져온다."""
    if lang == "en":
        locale = "hl=en-US&gl=US&ceid=US:en"
    else:
        locale = "hl=ko&gl=KR&ceid=KR:ko"
    url = f"https://news.google.com/rss/search?q={quote(query)}&{locale}"
    try:
        r = get(url)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        print(f"  구글 실패 ({query}): {e}", file=sys.stderr)
        return []

    out = []
    for item in root.findall("./channel/item")[:PER_QUERY]:
        title = clean_text(item.findtext("title"))
        source = clean_text(item.findtext("source")) or "구글뉴스"
        if title.endswith(f" - {source}"):
            title = title[: -len(source) - 3].strip()
        out.append({
            "title": title,
            "body": clean_text(item.findtext("description"))[:300],
            "url": item.findtext("link"),
            "source": source,
            "published": parse_date(item.findtext("pubDate")),
        })
    return out


def search(query, creds, market="kr"):
    """국내는 네이버, 해외는 구글 영문.

    미국 종목을 네이버에 물으면 한국 기사가 거의 없어 0건이 된다.
    반대로 국내 종목은 네이버가 원문 주소와 매체명을 정확히 준다.
    """
    if market == "us":
        rows = from_google(query, lang="en")
    elif creds["naver_id"]:
        rows = from_naver(query, creds)
    else:
        rows = from_google(query, lang="ko")
    time.sleep(0.25)
    return rows


def summarize(items, api_key):
    """한 줄 요약. 제목을 되풀이할 뿐인 본문은 버린다."""
    for it in items:
        body = (it.get("body") or "").strip()
        it["summary"] = "" if echoes_title(body, it["title"]) else body[:90]

    if not api_key or not items:
        return items

    numbered = "\n".join(f"{i+1}. {it['title']} / {(it.get('body') or '')[:150]}"
                         for i, it in enumerate(items))
    prompt = ("다음 뉴스들을 각각 한 문장으로 요약해라. "
              "숫자와 고유명사는 살리고 40자 이내로. 제목을 그대로 반복하지 말고 "
              "핵심 내용만. 설명 없이 '번호. 요약' 형식으로만 출력.\n\n" + numbered)
    try:
        r = requests.post(GEMINI_URL, params={"key": api_key}, timeout=60,
                          json={"contents": [{"parts": [{"text": prompt}]}]})
        r.raise_for_status()
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        print(f"  요약 폴백 ({e})", file=sys.stderr)
        return items

    for line in text.splitlines():
        line = line.strip()
        if not line or "." not in line[:4]:
            continue
        num, _, body = line.partition(".")
        try:
            idx = int(num.strip()) - 1
        except ValueError:
            continue
        if 0 <= idx < len(items) and body.strip():
            if not echoes_title(body, items[idx]["title"]):
                items[idx]["summary"] = body.strip()
    return items


def gather(queries, creds, excludes=(), limit=DEFAULT_MAX, market="kr"):
    cutoff = now_kst() - timedelta(hours=LOOKBACK_HOURS)
    raw = []
    for q in queries:
        raw.extend(search(q, creds, market))

    rows = []
    for it in raw:
        if not it.get("url") or not it.get("title"):
            continue
        if it["published"] and it["published"] < cutoff:
            continue
        if any(w and w in it["title"] for w in excludes):
            continue
        rows.append(it)

    rows.sort(key=lambda x: x["published"] or cutoff, reverse=True)
    kept = summarize(dedupe(rows)[:limit], creds["gemini"])
    for it in kept:
        it["published"] = it["published"].isoformat() if it["published"] else None
        it.pop("body", None)
    return kept


def main():
    creds = {
        "naver_id": env("NAVER_CLIENT_ID"),
        "naver_secret": env("NAVER_CLIENT_SECRET"),
        "gemini": env("GEMINI_API_KEY"),
    }
    if not creds["naver_id"]:
        print("네이버 키 없음 — 구글 뉴스 RSS로 동작합니다.", file=sys.stderr)

    total = 0

    picks = (read_json(DATA_DIR / "picks.json") or {}).get("picks", [])
    stocks = []
    for p in picks:
        market = p.get("market", "kr")
        queries = [p["name"]]
        if market == "us" and p.get("code"):
            # 회사명이 길거나 흔한 단어일 때를 대비해 티커도 같이 던진다
            queries.append(f'{p["code"]} stock')
        print(f"종목 뉴스: {p['name']} ({market})")
        items = gather(queries, creds, limit=DEFAULT_MAX, market=market)
        total += len(items)
        stocks.append({**p, "items": items})

    sectors = []
    for s in load_config("sectors.yaml")["sectors"]:
        print(f"섹터 뉴스: {s['name']}")
        items = gather(s.get("queries", []), creds,
                       excludes=s.get("exclude") or [],
                       limit=s.get("max", DEFAULT_MAX))
        total += len(items)
        sectors.append({"name": s["name"], "push": bool(s.get("push")),
                        "items": items})

    write_json(DATA_DIR / "news.json", {
        "updated_at": now_kst().isoformat(),
        "lookback_hours": LOOKBACK_HOURS,
        "total": total,
        "stocks": stocks,
        "sectors": sectors,
    })
    print(f"저장 완료 ({total}건)")


if __name__ == "__main__":
    main()
