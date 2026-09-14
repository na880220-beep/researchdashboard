"""news.json 에서 push: true 섹터만 골라 텔레그램으로 보낸다.

한 번 보낸 기사는 docs/data/sent.json 에 기록해 두고 다시 보내지 않는다.
이 파일은 워크플로에서 저장소로 커밋되므로 실행 사이에 기억이 유지된다.
"""

import html
import sys

import requests

from common import DATA_DIR, env, read_json, write_json

API = "https://api.telegram.org/bot{token}/sendMessage"
KEEP = 600  # 기억해 둘 기사 수


def send(token, chat_id, text):
    r = requests.post(
        API.format(token=token),
        timeout=20,
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
    )
    if not r.ok:
        print(f"발송 실패: {r.status_code} {r.text[:200]}", file=sys.stderr)
    return r.ok


def main():
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("텔레그램 설정이 없어 발송을 건너뜁니다.")
        return

    news = read_json(DATA_DIR / "news.json")
    if not news:
        print("news.json 이 없습니다. fetch_news.py 를 먼저 실행하세요.")
        return

    sent = read_json(DATA_DIR / "sent.json", default=[]) or []
    seen = set(sent)
    newly = []

    for sector in news["sectors"]:
        if not sector.get("push"):
            continue
        fresh = [it for it in sector["items"] if it["url"] not in seen]
        if not fresh:
            continue

        lines = [f"<b>{html.escape(sector['name'])}</b>"]
        for it in fresh:
            title = html.escape(it["title"])
            summary = html.escape(it.get("summary") or "")
            source = html.escape(it.get("source") or "")
            dup = f" +{it['dup_count']}" if it.get("dup_count") else ""
            lines.append(f'· <a href="{it["url"]}">{title}</a>')
            if summary:
                lines.append(f"  {summary}")
            lines.append(f"  <i>{source}{dup}</i>")
        if send(token, chat_id, "\n".join(lines)):
            newly.extend(it["url"] for it in fresh)

    if newly:
        write_json(DATA_DIR / "sent.json", (sent + newly)[-KEEP:])
    print(f"발송 {len(newly)}건")


if __name__ == "__main__":
    main()
