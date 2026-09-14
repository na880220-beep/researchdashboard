"""공용 유틸 — 설정 읽기, HTTP, 중복 제거, JSON 입출력."""

import json
import os
import re
import unicodedata
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "docs" / "data"
KST = timezone(timedelta(hours=9))

UA = "Mozilla/5.0 (compatible; research-dashboard/1.0)"


def now_kst():
    return datetime.now(KST)


def load_config(name):
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


def read_json(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, payload):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return p


def get(url, **kwargs):
    kwargs.setdefault("timeout", 15)
    headers = kwargs.pop("headers", {})
    headers.setdefault("User-Agent", UA)
    return requests.get(url, headers=headers, **kwargs)


TAG_RE = re.compile(r"<[^>]+>")
BRACKET_RE = re.compile(r"[\[\(【][^\]\)】]{0,20}[\]\)】]")
NONWORD_RE = re.compile(r"[^0-9a-z가-힣]+")


def clean_text(s):
    """HTML 태그와 엔티티를 걷어낸 평문."""
    if not s:
        return ""
    s = TAG_RE.sub("", s)
    for a, b in (("&quot;", '"'), ("&amp;", "&"), ("&lt;", "<"),
                 ("&gt;", ">"), ("&apos;", "'"), ("&#39;", "'"),
                 ("&nbsp;", " "), ("&middot;", "·"), ("&hellip;", "…")):
        s = s.replace(a, b)
    s = re.sub(r"\s+", " ", s)
    return unicodedata.normalize("NFKC", s).strip()


def title_key(title):
    """비교용으로 정규화한 제목 — 말머리와 기호를 제거."""
    s = BRACKET_RE.sub(" ", clean_text(title).lower())
    return NONWORD_RE.sub("", s)


def echoes_title(summary, title, threshold=0.7):
    """요약이 제목을 되풀이하고 있는지 — 구글 RSS 폴백에서 자주 발생한다."""
    a, b = title_key(summary), title_key(title)
    if not a or not b:
        return True
    if a.startswith(b[:24]) or b.startswith(a[:24]):
        return True
    return SequenceMatcher(None, a, b).ratio() >= threshold


def dedupe(items, threshold=0.82):
    """제목이 비슷한 기사를 하나로 접는다.

    가장 먼저 들어온 항목을 대표로 남기고, 접힌 개수를 dup_count 에 기록한다.
    같은 보도자료를 여러 매체가 받아쓰는 경우를 잡기 위한 것이라
    threshold 를 너무 낮추면 서로 다른 기사가 합쳐지니 주의.
    """
    kept = []
    for item in items:
        key = title_key(item.get("title", ""))
        if not key:
            continue
        match = None
        for k in kept:
            if key == k["_key"] or SequenceMatcher(None, key, k["_key"]).ratio() >= threshold:
                match = k
                break
        if match:
            match["dup_count"] = match.get("dup_count", 0) + 1
            continue
        item["_key"] = key
        item["dup_count"] = 0
        kept.append(item)
    for k in kept:
        k.pop("_key", None)
    return kept


def env(name, default=None):
    v = os.environ.get(name, "")
    return v.strip() or default
