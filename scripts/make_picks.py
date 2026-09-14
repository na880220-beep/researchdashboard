"""오늘 볼 종목 목록(picks.json)을 만든다.

지금은 config/watchlist.yaml 을 그대로 옮겨 담는 임시 버전이다.
나중에 스크리너가 완성되면 이 스크립트를 스크리너 출력으로 갈아끼우기만 하면 되고,
뉴스·리포트 수집과 화면은 손대지 않아도 된다.

picks.json 형식 (이 형식만 지키면 무엇이 만들든 상관없다)
  {
    "updated_at": "...",
    "picks": [
      {"code": "000660", "name": "SK하이닉스", "market": "kr",
       "source": "관심종목", "note": ""}
    ]
  }
"""

from common import DATA_DIR, load_config, now_kst, write_json

MAX_PICKS = 12  # 너무 많으면 수집 시간이 길어지고 알림도 시끄러워진다


def main():
    watchlist = load_config("watchlist.yaml")
    picks = []

    for row in watchlist.get("kr") or []:
        picks.append({
            "code": row["code"],
            "name": row["name"],
            "market": "kr",
            "source": "관심종목",
            "note": "",
        })

    for row in watchlist.get("us") or []:
        picks.append({
            "code": row["ticker"],
            "name": row.get("name") or row["ticker"],
            "market": "us",
            "source": "관심종목",
            "note": "",
        })

    picks = picks[:MAX_PICKS]
    write_json(DATA_DIR / "picks.json",
               {"updated_at": now_kst().isoformat(), "picks": picks})
    print(f"종목 {len(picks)}개: " + ", ".join(p["name"] for p in picks))


if __name__ == "__main__":
    main()
