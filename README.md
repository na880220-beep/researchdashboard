# 리서치 콘솔

개인용 리서치 대시보드. GitHub Actions 가 정해진 시각에 데이터를 모아
JSON 으로 저장하고, GitHub Pages 가 그 JSON 을 화면으로 보여줍니다.
서버도, 내 컴퓨터도 켜져 있을 필요가 없습니다.

현재 동작하는 것은 **섹터 뉴스** 하나이고, 나머지 네 패널은 자리만 잡혀 있습니다.

## 구조

```
config/sectors.yaml     관심 섹터와 검색어  ← 평소 고칠 파일은 사실상 이것뿐
config/watchlist.yaml   관심종목
scripts/                수집 스크립트
.github/workflows/      실행 시각표
docs/                   화면 (GitHub Pages 가 이 폴더를 공개)
docs/data/              스크립트가 만든 JSON
notebooks/              Colab 시험 실행용
```

## 설치 — 순서대로 따라가면 됩니다

### 1. 저장소 만들기
GitHub 에서 새 저장소를 만들고(이름 예: `research-dashboard`),
이 폴더의 파일을 그대로 올립니다. 웹에서 드래그로 업로드해도 됩니다.

### 2. 화면부터 켜기
저장소 **Settings → Pages** 에서
Source 를 `Deploy from a branch`, 브랜치를 `main`, 폴더를 `/docs` 로 지정합니다.
1~2분 뒤 `https://사용자명.github.io/research-dashboard/` 가 열립니다.
샘플 데이터가 들어 있어 이 시점에 이미 화면이 보입니다.

### 3. 열쇠 넣기
**Settings → Secrets and variables → Actions → New repository secret** 에서
필요한 것만 등록합니다. 하나도 없어도 뉴스 수집은 동작합니다.

| 이름 | 발급처 | 없으면 |
|---|---|---|
| `NAVER_CLIENT_ID` / `NAVER_CLIENT_SECRET` | developers.naver.com 에서 애플리케이션 등록 후 검색 API 추가 | 구글 뉴스 RSS 로 대체 |
| `GEMINI_API_KEY` | aistudio.google.com 에서 무료 발급 | 기사 앞부분을 잘라 요약 대신 사용 |
| `TELEGRAM_BOT_TOKEN` | 텔레그램에서 @BotFather 에게 `/newbot` | 발송 없이 화면에만 표시 |
| `TELEGRAM_CHAT_ID` | 봇에게 아무 말이나 보낸 뒤 `https://api.telegram.org/bot토큰/getUpdates` 접속 | 위와 같음 |

### 4. 한 번 돌려보기
**Actions 탭 → 섹터 뉴스 수집 → Run workflow.**
2분쯤 뒤 화면을 새로고침하면 샘플이 실제 기사로 바뀝니다.
이후로는 시각표대로 알아서 돕니다.

## 섹터 추가하기

`config/sectors.yaml` 에 항목을 하나 더 붙이면 끝입니다.

```yaml
  - name: 태양광·에너지정책
    queries: ["태양광 설치량", "IRA 세액공제", "인버터 수요"]
    exclude: ["분양", "광고"]
    push: false
    max: 5
```

- 검색어는 3~5개가 적당합니다. 늘릴수록 무관한 기사도 같이 늘어납니다.
- 회사 이름이 일상어와 겹치면(예: 한화, 대성) `exclude` 로 걸러내세요.
- `push: true` 인 섹터가 많아지면 알림이 쏟아집니다. 두세 개로 시작하세요.

## 패널 추가하기

1. `scripts/` 에 스크립트를 만들고 `docs/data/이름.json` 을 저장하게 합니다.
2. `.github/workflows/daily.yml` 의 배치 실행 단계에 한 줄 추가합니다.
3. `docs/index.html` 에서 그 JSON 을 읽어 그립니다.

`fetch_news.py` 가 이 세 단계의 예시이니 그대로 따라 하면 됩니다.

## 알아둘 것

- 무료 티어 범위 안에서 동작합니다. 퍼블릭 저장소면 Actions 시간 제한이 없고,
  프라이빗이면 월 2,000분 중 100분 남짓 씁니다.
- 저장소를 공개로 두면 관심종목과 섹터 설정이 남에게 보입니다. 신경 쓰이면 프라이빗으로.
- 예약 실행은 GitHub 사정으로 5~20분 늦어질 수 있습니다. 급한 알림은 공시 쪽으로 따로 받으세요.
- 60일간 커밋이 없으면 예약 실행이 자동으로 멈춥니다. 봇이 매시간 커밋하므로 실제로는 문제되지 않습니다.
