"""섹터 뉴스 수집기.

config/sectors.yaml 을 읽어 섹터별로 기사를 모으고, 중복을 접고,
한 줄 요약을 붙여 docs/data/news.json 으로 저장한다.

우선순위
  1. 네이버 뉴스 API — 키가 있으면 국내 기사는 여기서. 원문 주소를 그대로 준다.
  2. 구글 뉴스 RSS  — 키 없이 동작하는 폴백. 주소가 리다이렉트라 정확도는 낮다.

요약
  GEMINI_API_KEY 가 있으면 무료 티어로 한 줄 요약을 붙이고,
  없거나 실패하면 기사 본문 앞부분을 잘라 쓴다. 어느 쪽이든 화면은 똑같이 나온다.
"""

import sys
import time
import xml.etree.ElementTree as ET
from datetime import timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests

from common import (DATA_DIR, KST, clean_text, dedupe, env, get, load_config,
                    now_kst, write_json)

LOOKBACK_HOURS = 36
GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
              "gemini-2.0-flash:generateContent")


def parse_date(value):
    try:
        return parsedate_to_datetime(value).astimezone(KST)
    except Exception:
        return None


def from_naver(query, client_id, client_secret):
    url = "https://openapi.naver.com/v1/search/news.json"
    params = {"query": query, "display": 20, "sort": "date"}
    headers = {"X-Naver-Client-Id": client_id,
               "X-Naver-Client-Secret": client_secret}
    try:
        r = get(url, params=params, headers=headers)
        r.raise_for_status()
        rows = r.json().get("items", [])
    except Exception as e:
        print(f"  naver 실패 ({query}): {e}", file=sys.stderr)
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
            "query": query,
        })
    return out


def from_google(query):
    url = ("https://news.google.com/rss/search?q="
           f"{quote(query)}&hl=ko&gl=KR&ceid=KR:ko")
    try:
        r = get(url)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        print(f"  google 실패 ({query}): {e}", file=sys.stderr)
        return []

    out = []
    for item in root.findall("./channel/item"):
        title = clean_text(item.findtext("title"))
        source = clean_text(item.findtext("source")) or "구글뉴스"
        # 구글은 제목 끝에 " - 매체명"을 붙인다
        if title.endswith(f" - {source}"):
            title = title[: -len(source) - 3].strip()
        out.append({
            "title": title,
            "body": clean_text(item.findtext("description"))[:300],
            "url": item.findtext("link"),
            "source": source,
            "published": parse_date(item.findtext("pubDate")),
            "query": query,
        })
    return out


def source_from_url(url):
    if not url:
        return ""
    host = url.split("//")[-1].split("/")[0].replace("www.", "")
    return host.split(".")[0]


def summarize(items, api_key):
    """한 줄 요약을 채운다. 실패하면 본문 앞부분으로 폴백."""
    for it in items:
        it["summary"] = (it.get("body") or "")[:90].rstrip()

    if not api_key or not items:
        return items

    numbered = "\n".join(f"{i+1}. {it['title']} / {it['body'][:150]}"
                         for i, it in enumerate(items))
    prompt = (
        "다음 뉴스들을 각각 한 문장으로 요약해라. "
        "숫자와 고유명사는 살리고, 40자 이내로. "
        "설명 없이 '번호. 요약' 형식으로만 출력.\n\n" + numbered
    )
    try:
        r = requests.post(
            GEMINI_URL, params={"key": api_key}, timeout=40,
            json={"contents": [{"parts": [{"text": prompt}]}]},
        )
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
            items[idx]["summary"] = body.strip()
    return items


def collect_sector(sector, creds):
    cutoff = now_kst() - timedelta(hours=LOOKBACK_HOURS)
    raw = []
    for query in sector.get("queries", []):
        if creds["naver_id"]:
            rows = from_naver(query, creds["naver_id"], creds["naver_secret"])
        else:
            rows = from_google(query)
        raw.extend(rows)
        time.sleep(0.3)

    excludes = sector.get("exclude") or []
    filtered = []
    for it in raw:
        if not it["url"] or not it["title"]:
            continue
        if it["published"] and it["published"] < cutoff:
            continue
        if any(word and word in it["title"] for word in excludes):
            continue
        filtered.append(it)

    filtered.sort(key=lambda x: x["published"] or cutoff, reverse=True)
    kept = dedupe(filtered)[: sector.get("max", 6)]
    kept = summarize(kept, creds["gemini"])

    for it in kept:
        it["published"] = it["published"].isoformat() if it["published"] else None
        it.pop("body", None)
        it.pop("query", None)
    return kept


def main():
    creds = {
        "naver_id": env("NAVER_CLIENT_ID"),
        "naver_secret": env("NAVER_CLIENT_SECRET"),
        "gemini": env("GEMINI_API_KEY"),
    }
    if not creds["naver_id"]:
        print("네이버 키 없음 — 구글 뉴스 RSS로 동작합니다.", file=sys.stderr)

    config = load_config("sectors.yaml")
    sectors, total = [], 0
    for sector in config["sectors"]:
        print(f"수집: {sector['name']}")
        items = collect_sector(sector, creds)
        total += len(items)
        sectors.append({
            "name": sector["name"],
            "push": bool(sector.get("push")),
            "items": items,
        })

    payload = {
        "updated_at": now_kst().isoformat(),
        "lookback_hours": LOOKBACK_HOURS,
        "total": total,
        "sectors": sectors,
    }
    path = write_json(DATA_DIR / "news.json", payload)
    print(f"저장 완료: {path} ({total}건)")


if __name__ == "__main__":
    main()
