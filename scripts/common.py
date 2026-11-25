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


def has_claude():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def call_claude(prompt, model, max_tokens=1000, timeout=120):
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    res = json.loads(fetch("https://api.anthropic.com/v1/messages", {
        "x-api-key": "".join(os.environ["ANTHROPIC_API_KEY"].split()),
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"}, timeout=timeout, data=body))
    return "".join(b.get("text", "") for b in res["content"])


def parse_json(text):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M)
    return json.loads(text[text.find("{"): text.rfind("}") + 1])


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
    save_json(DATA_DIR / "index.json",
              {"updated": now_kst().isoformat(timespec="minutes"), "entries": entries})
