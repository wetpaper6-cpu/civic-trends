#!/usr/bin/env python3
"""시민사회 동향 수집기

1) 네이버 뉴스 검색 API: '단체명 + 키워드' 조합으로 최근 기사 수집
2) 단체 홈페이지: config.json의 pages에서 키워드가 들어간 게시글 링크 수집
3) 같은 사안을 다룬 기사끼리 묶고, 본문을 읽어 아래 양식으로 정리
     [유형] 제목 / 일시·장소 / 주최 기관 / 주요 내용
   - 기본: 규칙 기반 추출 (API 키 불필요)
   - ANTHROPIC_API_KEY가 있으면 Claude가 본문을 읽고 더 정확하게 정리

결과: docs/data/YYYY-MM-DD.json, docs/data/index.json, build/release-notes.md
표준 라이브러리만 사용합니다.
"""
import html
import json
import os
import re
from datetime import datetime, time, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlparse

from common import (CONFIG, DATA_DIR, BUILD_DIR, KST, call_claude, fetch, fmt_date,
                    has_claude, load_json, log, now_kst, parse_json, period_label,
                    report_date, save_json, build_index)

SEEN_FILE = DATA_DIR / "seen_homepage.json"
TYPES = ["기자회견", "성명", "논평"]  # 판별 우선순위


def strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def squash(s):
    return " ".join((s or "").split())


# ================================================================ 수집
def search_naver(org, keyword, since):
    query = f"{org['name']} {keyword}"
    # NAVER API HUB (네이버 클라우드 플랫폼). 기존 openapi.naver.com 검색 API는 이관됨
    url = ("https://naverapihub.apigw.ntruss.com/search/v1/news"
           f"?query={quote(query)}&display=100&sort=date")
    data = json.loads(fetch(url, {
        "X-NCP-APIGW-API-KEY-ID": "".join(os.environ["NAVER_CLIENT_ID"].split()),
        "X-NCP-APIGW-API-KEY": "".join(os.environ["NAVER_CLIENT_SECRET"].split()),
    }))
    for it in data.get("items", []):
        try:
            pub = parsedate_to_datetime(it["pubDate"])
        except (KeyError, TypeError, ValueError):
            continue
        if pub < since:
            continue
        title = squash(strip_tags(it.get("title")))
        desc = squash(strip_tags(it.get("description")))
        if not any(a in f"{title} {desc}" for a in org["aliases"]):
            continue
        link = it.get("originallink") or it.get("link")
        yield {
            "source": "naver", "org": org["name"], "keywords": [keyword],
            "title": title, "snippet": desc, "url": link,
            "naver_url": it.get("link"),
            "press": urlparse(link).netloc.replace("www.", ""),
            "published": pub.astimezone(KST),
        }


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self._href, self._buf = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._buf = []

    def handle_data(self, data):
        if self._href is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, squash("".join(self._buf))))
            self._href = None


def scrape_homepage(org, keywords, seen, now):
    for page in org.get("pages", []):
        try:
            body = fetch(page)
        except Exception as e:
            log(f"[홈페이지 실패] {org['name']} {page}: {e}")
            continue
        p = LinkParser()
        p.feed(body)
        for href, text in p.links:
            if not href or href.startswith(("javascript:", "#", "mailto:")) or len(text) < 6:
                continue
            hits = [k for k in keywords if k in text]
            if not hits:
                continue
            url = urljoin(page, href)
            if url in seen:
                continue
            seen.add(url)
            yield {
                "source": "homepage", "org": org["name"], "keywords": hits,
                "title": text, "snippet": "", "url": url, "naver_url": None,
                "press": org["name"] + " 홈페이지", "published": now,
            }


# ================================================================ 사안 묶기
def bigrams(s):
    s = re.sub(r"[^가-힣A-Za-z0-9]", "", s)
    return {s[i:i + 2] for i in range(len(s) - 1)}


def similar(a, b, th=0.4):
    A, B = bigrams(a), bigrams(b)
    return bool(A and B) and len(A & B) / len(A | B) >= th


def group_events(items):
    """같은 단체의 비슷한 제목 기사를 하나의 사안으로 묶음 (홈페이지 원문 우선)"""
    events = []
    items = sorted(items, key=lambda x: (x["source"] != "homepage", x["published"]))
    for it in items:
        for ev in events:
            if ev["org"] == it["org"] and similar(ev["main"]["title"], it["title"]):
                ev["articles"].append(it)
                ev["keywords"] += [k for k in it["keywords"] if k not in ev["keywords"]]
                break
        else:
            events.append({"org": it["org"], "keywords": list(it["keywords"]),
                           "main": it, "articles": [it]})
    return events


CLUSTER_PROMPT = """아래는 시민사회단체 관련 뉴스 기사·게시글 목록입니다. 두 가지를 해 주세요.
1) 같은 행사(같은 기자회견·집회, 같은 성명·논평)를 다룬 항목끼리 묶기. 언론사마다 제목이 달라도 같은 행사면 한 묶음입니다.
2) 단체가 직접 연 기자회견·집회나 직접 발표한 성명·논평·보도자료가 아닌 항목은 제외 목록에 넣기
   (단체가 잠깐 언급만 된 기사, 칼럼, 인사·동정, 단체와 무관한 동명 기사 등)

JSON으로만 답하세요: {{"groups": [[번호, ...], ...], "drop": [번호, ...]}}
모든 번호는 groups 또는 drop 중 한 곳에 한 번씩만 넣으세요.

목록
{items}"""


def cluster_llm(groups):
    """Claude로 같은 사안 묶기 + 관련 없는 기사 제외"""
    lines = [f"{i}. [{g['org']}] {g['main']['title']} | {g['main']['snippet'][:120]}"
             for i, g in enumerate(groups, 1)]
    res = parse_json(call_claude(CLUSTER_PROMPT.format(items="\n".join(lines)),
                                 CONFIG.get("model_daily", "claude-haiku-5-5"), 4000, 180))
    n = len(groups)
    drop = {i for i in res.get("drop", []) if isinstance(i, int)}
    used, merged = set(), []
    for grp in res.get("groups", []):
        ids = [i for i in grp if isinstance(i, int) and 1 <= i <= n and i not in used and i not in drop]
        if not ids:
            continue
        used.update(ids)
        members = [groups[i - 1] for i in ids]
        # 대표 기사: 단체 홈페이지 원문 > 가장 많이 보도된 묶음
        base = max(members, key=lambda g: (g["main"]["source"] == "homepage", len(g["articles"])))
        ev = {"org": base["org"], "keywords": [], "main": base["main"], "articles": []}
        for g in members:
            ev["articles"] += g["articles"]
            ev["keywords"] += [k for k in g["keywords"] if k not in ev["keywords"]]
        merged.append(ev)
    merged += [g for i, g in enumerate(groups, 1) if i not in used and i not in drop]
    log(f"Claude 묶기: {n}건 → {len(merged)}건 (제외 {len(drop)}건)")
    return merged


# ================================================================ 본문·항목 추출
def article_text(url, limit=4000):
    try:
        page = fetch(url, timeout=12)
    except Exception as e:
        log(f"[본문 실패] {url}: {e}")
        return ""
    page = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", page)
    m = re.search(r'(?is)<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)', page)
    og = html.unescape(m.group(1)) if m else ""
    paras = [squash(strip_tags(p)) for p in re.findall(r"(?is)<p[^>]*>(.*?)</p>", page)]
    body = " ".join(p for p in paras if len(p) > 30)
    if len(body) < 200:  # <p>를 안 쓰는 언론사: 기사 영역 통째로
        m = re.search(r"(?is)<(article|div[^>]+(?:article|news|content)[^>]*)>(.*?)</(article|div)>", page)
        body = squash(strip_tags(m.group(2))) if m else body
    return squash(f"{og} {body}")[:limit]


def detect_type(text, keywords):
    for t in TYPES:
        if t in text:
            return t
    for t in TYPES:
        if t in keywords:
            return t
    return "보도"


PLACE_RE = re.compile(
    r"([가-힣A-Za-z0-9·()]+(?:\s[가-힣A-Za-z0-9·()]+){0,3}?"
    r"\s?(?:앞|광장|분수대|소통관|정론관|기자실|회의실|프레스센터|사거리|본관|로비|정문|일대))"
    r"(?:\s?에서)")
TIME_RE = re.compile(
    r"(?:(\d{1,2})월\s*)?(\d{1,2})일\s*(오전|오후)?\s*(\d{1,2})시(?:\s*(\d{1,2})분|\s*(반))?")


def clean_place(p):
    """'협동사무처장은 24일 오전 11시 국회 소통관' → '국회 소통관'
    마지막 날짜·시각·주어(…은/는) 표현 뒤의 말만 장소로 씀"""
    tokens = p.split()
    cut = 0
    for i, t in enumerate(tokens[:-1]):
        if re.search(r"\d", t) or re.match(r"^(오전|오후|반|등|\S*[은는이가])$", t):
            cut = i + 1
    return " ".join(tokens[cut:])


def extract_rules(ev, text):
    main = ev["main"]
    pub = main["published"]
    kind = detect_type(f"{main['title']} {text[:300]}", ev["keywords"])

    when = fmt_date(pub)
    m = TIME_RE.search(text)
    if m:
        month = int(m.group(1) or pub.month)
        day = int(m.group(2))
        try:
            d = pub.replace(month=month, day=day)
            if abs((d - pub).days) <= 7:
                ampm = m.group(3) or ""
                minute = " 30분" if m.group(6) else (f" {int(m.group(5))}분" if m.group(5) else "")
                when = f"{fmt_date(d)} {ampm} {int(m.group(4))}시{minute}".replace("  ", " ")
        except ValueError:
            pass

    place = ""
    if kind == "기자회견":
        m = PLACE_RE.search(text)
        place = clean_place(m.group(1)) if m else "확인 필요"

    hosts = [o["name"] for o in CONFIG["orgs"] if any(a in text or a in main["title"] for a in o["aliases"])]
    if ev["org"] not in hosts:
        hosts.insert(0, ev["org"])

    sents = re.split(r"(?<=[다요]\.)\s+", text)
    summary = " ".join(s.strip() for s in sents[:2] if s.strip())
    summary = (summary or main["snippet"])[:300]

    return {"type": kind, "title": main["title"], "datetime": when,
            "place": place, "hosts": ", ".join(hosts), "summary": summary}


LLM_PROMPT = """다음은 한국 시민사회단체의 활동을 다룬 기사(또는 단체 게시글)입니다.
아래 JSON 형식으로만 답하세요. 다른 말은 쓰지 마세요.

{{"type": "기자회견|성명|논평|집회|보도 중 하나",
 "title": "사안을 나타내는 간결한 제목 (언론사 제목의 과장 표현은 빼고)",
 "datetime": "행사 일시, 예: 2026.11.18(수) 오전 11시. 성명·논평은 발표일",
 "place": "기자회견·집회 장소. 그 외 유형이거나 알 수 없으면 빈 문자열",
 "hosts": "주최·발표 기관을 쉼표로 (공동주최 포함)",
 "summary": "핵심 주장과 요구를 2~3문장으로"}}

기준 단체: {org}
기사 게시 시각: {published}
제목: {title}
본문: {text}"""


def extract_llm(ev, text):
    main = ev["main"]
    prompt = LLM_PROMPT.format(
        org=ev["org"], published=main["published"].strftime("%Y-%m-%d %H:%M"),
        title=main["title"], text=text or main["snippet"])
    return parse_json(call_claude(prompt, CONFIG.get("model_daily", "claude-haiku-5-5"), 600, 60))


def enrich(ev):
    main = ev["main"]
    text = article_text(main["url"]) or main["snippet"]
    info = extract_rules(ev, text)
    if has_claude():
        try:
            llm = extract_llm(ev, text)
            info.update({k: squash(str(v)) for k, v in llm.items() if k in info and v})
            if info["type"] not in ("기자회견", "집회"):
                info["place"] = ""
        except Exception as e:
            log(f"[Claude 정리 실패, 규칙 기반 사용] {main['title']}: {e}")
    sources = [{"title": a["title"], "url": a["url"], "press": a["press"]} for a in ev["articles"]]
    return {**info, "org": ev["org"], "keywords": ev["keywords"],
            "published": main["published"].isoformat(timespec="minutes"),
            "url": main["url"], "sources": sources}


# ================================================================ 출력
def release_notes(day):
    lines = [f"# 시민사회 동향 · {day['period_label']}", "",
             f"총 {day['count']}건 · 키워드: {', '.join(day['keywords'])}", "",
             "📄 Word·PDF 파일은 맨 아래 **Assets**에서 내려받을 수 있습니다.", ""]
    for ev in day["events"]:
        when = ev["datetime"] + (f" / {ev['place']}" if ev["place"] else "")
        label = "일시 / 장소" if ev["place"] else "일시"
        first = ev["sources"][0]
        more = f" 외 {len(ev['sources']) - 1}건" if len(ev["sources"]) > 1 else ""
        lines += [
            f"### [{ev['type']}] {ev['title']}",
            f"**{label} :** {when}  ",
            f"**주최 기관 :** {ev['hosts']}  ",
            f"**주요 내용 :** {ev['summary']}  ",
            f"<sub>출처: [{first['press']}]({first['url']}){more}</sub>",
            "", "---", "",
        ]
    if not day["events"]:
        lines.append("해당 기간에 수집된 소식이 없습니다.")
    return "\n".join(lines)


def previous_urls(today, n=3):
    urls = set()
    files = sorted(p for p in DATA_DIR.glob("????-??-??.json") if p.stem < today.isoformat())
    for p in files[-n:]:
        for ev in load_json(p)["events"]:
            urls.update(s["url"] for s in ev["sources"])
    return urls


def main():
    now = now_kst()
    today = report_date()
    end = datetime.combine(today, time(23, 59), KST) if today != now.date() else now
    # 일요일: 금~일 3일치, 그 외: 하루치 (실행 지연 대비 2시간 여유, 중복은 아래에서 제거)
    if today.weekday() == 6:
        start_day = today - timedelta(days=2)
        since = end - timedelta(hours=74)
    else:
        start_day = today
        since = end - timedelta(hours=26)
    keywords = CONFIG["keywords"]
    already = previous_urls(today)

    items = {}

    def add(item):
        if item["url"] in already:
            return
        if item["url"] in items:
            ks = items[item["url"]]["keywords"]
            ks += [k for k in item["keywords"] if k not in ks]
        else:
            items[item["url"]] = item

    if os.environ.get("NAVER_CLIENT_ID") and os.environ.get("NAVER_CLIENT_SECRET"):
        naver_ok = True
        for org in CONFIG["orgs"]:
            for kw in keywords:
                try:
                    for it in search_naver(org, kw, since):
                        if it["published"] <= end:
                            add(it)
                except Exception as e:
                    detail = ""
                    if hasattr(e, "read"):  # 네이버가 보낸 상세 사유
                        try:
                            detail = " | " + e.read().decode("utf-8", "replace")[:300]
                        except Exception:
                            pass
                    log(f"[네이버 실패] {org['name']} {kw}: {e}{detail}")
                    if getattr(e, "code", None) in (401, 403):
                        log("인증 오류라 나머지 네이버 검색을 건너뜁니다.")
                        naver_ok = False
                        break
            if not naver_ok:
                break
    else:
        log("NAVER_CLIENT_ID/SECRET 없음: 네이버 검색을 건너뜁니다.")

    seen = set(load_json(SEEN_FILE)) if SEEN_FILE.exists() else set()
    first_run = not SEEN_FILE.exists()
    for org in CONFIG["orgs"]:
        for it in scrape_homepage(org, keywords, seen, now):
            if not first_run:  # 첫 실행 땐 기존 글을 '본 것'으로만 기록
                add(it)
    SEEN_FILE.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=0))

    groups = group_events(list(items.values()))
    groups.sort(key=lambda g: len(g["articles"]), reverse=True)
    groups = groups[:200]
    if has_claude() and len(groups) > 1:
        try:
            groups = cluster_llm(groups)
        except Exception as e:
            log(f"[Claude 묶기 실패, 제목 유사도로만 묶음] {e}")
    events = [enrich(g) for g in groups[:CONFIG.get("max_events", 80)]]
    order = {t: i for i, t in enumerate(TYPES + ["집회", "보도"])}
    events.sort(key=lambda e: (order.get(e["type"], 9), e["org"], e["published"]))

    ds = today.isoformat()
    day = {
        "kind": "daily", "date": ds,
        "period_start": start_day.isoformat(), "period_end": ds,
        "period_label": period_label(start_day, today),
        "generated_at": now.isoformat(timespec="minutes"),
        "count": len(events), "keywords": keywords,
        "orgs": [o["name"] for o in CONFIG["orgs"]], "events": events,
        "files": {"docx": f"files/daily-{ds}.docx", "pdf": f"files/daily-{ds}.pdf"},
    }
    save_json(DATA_DIR / f"{ds}.json", day)
    build_index()
    (BUILD_DIR / "daily-notes.md").write_text(release_notes(day), encoding="utf-8")
    (BUILD_DIR / "daily-title.txt").write_text(f"시민사회 동향 {day['period_label']}", encoding="utf-8")
    log(f"완료: {day['period_label']} 기사 {len(items)}건 → 사안 {len(events)}건")


if __name__ == "__main__":
    main()
