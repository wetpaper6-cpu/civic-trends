"""공통 설정과 도구"""
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
DATA_DIR = ROOT / "docs" / "data"
FILES_DIR = ROOT / "docs" / "files"
BUILD_DIR = ROOT / "build"
WEEKDAYS = "월화수목금토일"
UA = "Mozilla/5.0 (compatible; civic-trends-bot/1.0)"

for d in (DATA_DIR, FILES_DIR, BUILD_DIR):
    d.mkdir(parents=True, exist_ok=True)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def now_kst():
    return datetime.now(KST)


def report_date():
    """REPORT_DATE=YYYY-MM-DD 로 날짜를 지정할 수 있음 (기본: 오늘)"""
    v = os.environ.get("REPORT_DATE")
    return date.fromisoformat(v) if v else now_kst().date()


def fmt_date(d):
    return f"{d.year}.{d.month:02d}.{d.day:02d}({WEEKDAYS[d.weekday()]})"


def period_label(start, end):
    return fmt_date(end) if start == end else f"{fmt_date(start)} ~ {fmt_date(end)}"


def fetch(url, headers=None, timeout=20, data=None):
    req = Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    with urlopen(req, timeout=timeout) as r:
        raw = r.read()
        charset = r.headers.get_content_charset()
    for enc in [charset, "utf-8", "cp949"]:
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            pass
    return raw.decode("utf-8", "replace")


_claude_off = False


def has_claude():
    return bool(os.environ.get("ANTHROPIC_API_KEY")) and not _claude_off


def call_claude(prompt, model, max_tokens=1000, timeout=120, thinking=None):
    """thinking: None이면 모델 기본값(생각 켜짐). 짧은 작업은 {"type": "disabled"}"""
    global _claude_off
    try:
        return _call_claude(prompt, model, max_tokens, timeout, thinking)
    except Exception as e:
        code = getattr(e, "code", None)
        if code in (401, 403) or (code == 400 and "credit" in str(getattr(e, "_body", "")).lower()):
            _claude_off = True
            log(f"Claude API 키 오류({code}): 이번 실행에서는 Claude 정리를 끕니다. "
                "ANTHROPIC_API_KEY 값과 크레딧을 확인하세요.")
        raise


def _call_claude(prompt, model, max_tokens, timeout, thinking):
    req = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if thinking:
        req["thinking"] = thinking
    body = json.dumps(req).encode()
    raw = fetch("https://api.anthropic.com/v1/messages", {
        "x-api-key": "".join(os.environ["ANTHROPIC_API_KEY"].split()),
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"}, timeout=timeout, data=body)
    try:
        res = json.loads(raw)
    except ValueError:
        log(f"[Claude 응답 해석 불가] {raw[:300]!r}")
        raise
    text = "".join(b.get("text", "") for b in res.get("content", []) if b.get("type") == "text")
    if not text.strip():
        log(f"[Claude 빈 응답] stop_reason={res.get('stop_reason')} "
            f"blocks={[b.get('type') for b in res.get('content', [])]} model={res.get('model')}")
    return text


def as_list(v):
    """개조식 항목 목록 (예전 형식의 문단 문자열도 처리)"""
    if isinstance(v, list):
        items = [str(x) for x in v]
    else:
        items = str(v or "").split("\n")
    return [x.strip().lstrip("○-·•").strip() for x in items if x.strip().lstrip("○-·•").strip()]


def as_text(v):
    """주요 내용을 한 덩어리 글로 (예전 목록 형식도 이어 붙임)"""
    return " ".join(as_list(v)) if isinstance(v, list) else " ".join(str(v or "").split())


def parse_json(text):
    if "{" not in text:
        log(f"[Claude 응답에 JSON 없음] {text[:300]!r}")
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M)
    # 첫 JSON 객체만 읽음 (뒤에 설명이 붙어 와도 괜찮게)
    obj, _ = json.JSONDecoder().raw_decode(text[text.find("{"):])
    return obj


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_json(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def build_index():
    """사이트 첫 화면에 쓰는 날짜 목록"""
    entries = []
    for p in sorted(DATA_DIR.glob("????-??-??.json"), reverse=True):
        d = load_json(p)
        entries.append({
            "date": d["date"],
            "label": d["period_label"],
            "count": d["count"],
            "weekly": (DATA_DIR / f"weekly-{d['date']}.json").exists(),
        })
    first = CONFIG.get("weekly_first_date", "")
    notice = ""
    if first:
        d = date.fromisoformat(first)
        notice = f"첫 주간 동향은 {d.month}월 {d.day}일({WEEKDAYS[d.weekday()]}) 저녁 7시에 올라옵니다."
    save_json(DATA_DIR / "index.json",
              {"updated": now_kst().isoformat(timespec="minutes"), "entries": entries,
               "weekly_notice": notice})
