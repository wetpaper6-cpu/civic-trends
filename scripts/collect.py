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

from common import (as_list, as_text, CONFIG, DATA_DIR, BUILD_DIR, KST, call_claude, fetch, fmt_date,
                    has_claude, load_json, log, now_kst, parse_json, period_label,
                    report_date, save_json, build_index)

SEEN_FILE = DATA_DIR / "seen_homepage.json"
TYPES = ["기자회견", "성명", "논평"]  # 판별 우선순위


def strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def squash(s):
    return " ".join((s or "").split())


# ================================================================ 수집
OTHER = "기타 단체"  # 지정 6개 단체 외


def search_naver(query, since, org_name, aliases, keyword):
    """aliases가 있으면 제목·요약에 단체명이 나오는 기사만 남김"""
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
        if aliases and not any(a in f"{title} {desc}" for a in aliases):
            continue
        link = it.get("originallink") or it.get("link")
        yield {
            "source": "naver", "org": org_name, "keywords": [keyword],
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


CLUSTER_PROMPT = """아래는 시민사회 관련 뉴스 기사·게시글 목록입니다. 대상 기간: {period}
같은 행사(같은 기자회견·집회, 같은 성명·논평)를 다룬 항목끼리 번호를 묶어 주세요.
언론사마다 제목이 달라도 같은 행사면 한 묶음입니다. 관련 없어 보이는 항목도 빼지 말고 혼자 한 묶음으로 두세요.

JSON으로만 답하세요: {{"groups": [[번호, ...], ...]}}  (모든 번호를 한 번씩)

목록
{items}"""


def cluster_llm(groups, period):
    """Claude로 같은 사안 묶기 + 관련 없는 기사 제외"""
    lines = [f"{i}. [{g['org']}] {g['main']['title']} | {g['main']['snippet'][:120]}"
             for i, g in enumerate(groups, 1)]
    res = parse_json(call_claude(CLUSTER_PROMPT.format(period=period, items="\n".join(lines)),
                                 CONFIG.get("model_daily", "claude-haiku-5-5"), 4000, 180,
                                 thinking={"type": "disabled"}))
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
    m = (re.search(r'(?is)<meta[^>]+(?:article:published_time|og:regDate|pubdate|date)["\'][^>]+content=["\']([^"\']+)', page)
         or re.search(r'(?is)<time[^>]+datetime=["\']([^"\']+)', page))
    if m:
        og = f"[게시일 {m.group(1)[:10]}] {og}"
    else:  # 메타 정보가 없으면 페이지에 보이는 날짜들을 힌트로 (게시판 상단 날짜 등)
        plain = strip_tags(page)
        found = []
        for y, mo, d in re.findall(r"(20\d\d)[.\-/]\s?(\d{1,2})[.\-/]\s?(\d{1,2})", plain):
            v = f"{y}-{int(mo):02d}-{int(d):02d}"
            if v not in found:
                found.append(v)
        if found:
            og = f"[페이지에 보이는 날짜: {', '.join(found[:3])}] {og}"
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

    return {"type": kind, "date": "", "scope": "", "title": main["title"], "datetime": when,
            "place": place, "hosts": ", ".join(hosts), "summary": summary}


LLM_PROMPT = """다음은 한국 시민사회 활동을 다룬 기사(또는 단체 게시글)입니다.
아래 JSON 형식으로만 답하세요. 다른 말은 쓰지 마세요.

{{"type": "기자회견|성명|논평|집회 중 하나",
 "date": "행사일(성명·논평은 단체가 처음 발표한 날), YYYY-MM-DD. 기사가 오늘 나왔어도 본문상 발표·행사가 그 전이면 그 날짜. 확인할 수 없으면 빈 문자열",
 "title": "사안을 나타내는 간결한 제목 (언론사 제목의 과장 표현은 빼고)",
 "datetime": "행사 일시, 예: 2026.11.18(수) 오전 11시. 성명·논평은 발표일",
 "place": "기자회견·집회 장소. 그 외 유형이거나 알 수 없으면 빈 문자열",
 "hosts": "주최·발표 기관을 쉼표로 (공동주최 포함)",
 "scope": "전국 또는 지역. 전국 = 국가 정책·법안·국회·중앙정부·전국적 사회문제를 다룸 (지역 단체가 지역에서 열었어도 전국). 지역 = 특정 지자체 정책·지역 시설·지역 인사 등 그 지역에 한정된 현안",
 "summary": "핵심 주장·요구를 1~2문장으로. 개조식 어미로 끝맺기(예: '~ 철회 촉구', '~ 요구', '~ 비판', '~ 지적'). '~했다', '~이다' 같은 서술형 금지. 문장이 둘이면 '~ 지적하며 ~ 촉구'처럼 잇거나 각각 명사형으로 끝맺기"}}

다음 중 하나라도 해당하면 {{"skip": true}} 만 답하세요.
- 단체가 직접 연 기자회견·집회나 직접 발표한 성명·논평이 아님 (일정 목록, 단순 언급·해설 기사 등)
- 서울 이외 지역의 지역 현안임 (지역 단체가 연 것이라도 국가 정책·법안 등 전국 이슈면 skip하지 말 것)
- 정당·정당 소속 정치인·정부·지자체·기업이 주체임
- 특정 기업·사업장 노동조합의 노사 현안임 (합병, 구조조정, 임금·단체협약, 파업, 본사 이전 등)
- 기준 단체가 '{other}'이면: 시민사회단체·노동단체(총연맹·산별노조)·학생·청년·종교·전문가·직능 단체(소상공인연합회 등)가 국가 정책이나 사회 현안에 대해 낸 것은 포함하고(정당이 함께 주최해도 포함), 그 밖의 것은 skip

기준 단체: {org}
기사 게시 시각: {published}
제목: {title}
본문: {text}"""


def extract_llm(ev, text):
    main = ev["main"]
    prompt = LLM_PROMPT.format(
        org=ev["org"], other=OTHER,
        published=("알 수 없음 (단체 홈페이지 게시글: 본문의 날짜로 판단)" if main["source"] == "homepage"
                   else main["published"].strftime("%Y-%m-%d %H:%M")),
        title=main["title"], text=text or main["snippet"])
    return parse_json(call_claude(prompt, CONFIG.get("model_daily", "claude-haiku-5-5"), 1000, 60,
                                  thinking={"type": "disabled"}))


def fix_date(text, pub):
    """Claude가 쓴 날짜의 연도·요일을 기사 게시일 기준으로 바로잡음"""
    from datetime import date as _d

    def repl(m):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            cand = _d(y, mo, d)
            if abs((cand - pub.date()).days) > 180:
                cand = _d(pub.year, mo, d)
        except ValueError:
            return m.group(0)
        return fmt_date(cand)
    return re.sub(r"(\d{4})\.\s?(\d{1,2})\.\s?(\d{1,2})\.?(?:\s?\([^)]*\))?", repl, text or "")


REGIONS = "부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|충청|전북|전남|전라|경북|경남|경상|제주|수원|창원|청주|전주|포항|김해|성남|고양|용인"
REGIONAL_PLACE = re.compile(rf"^(?!서울)\S*({REGIONS})")
REGIONAL_HOST = re.compile(rf"^({REGIONS})")  # 대구참여연대, 부산경실련 등 지역 단체
REGIONAL_BRANCH = re.compile(rf"({REGIONS})\S*(본부|지부|지회|지역본부)")  # 민주노총 대전본부 등 전국 조직의 지역 조직
PARTIES = re.compile(r"국민의힘|더불어민주당|민주당|조국혁신당|개혁신당|진보당|정의당|기본소득당|사회민주당|노동당|녹색당|새미래|더민초|당협|원내대표|국회의원|의원")


CIVIC_WORDS = re.compile(r"시민|연대|행동|단체|본부|노조|노동조합|연합|협의회|네트워크|운동|모임|공동|유니온")


def is_party(h):
    """정당·정치인이 주체인 이름만 (시민행동 이름에 '민주당'이 들어간 경우 등은 제외)"""
    if CIVIC_WORDS.search(h):
        return False
    return bool(re.match(r"^(국민의힘|더불어민주당|민주당|조국혁신당|개혁신당|진보당|정의당|기본소득당|사회민주당|노동당|녹색당|새미래)", h)
                or re.search(r"의원|더민초|당협|원내대표", h))


def excluded(e):
    """지방의 지역 현안·정당 주최는 제외. 지방에서 열렸어도 전국 이슈(scope=전국)면 포함"""
    national = e.get("scope") == "전국"
    if not national and e["place"] and REGIONAL_PLACE.search(e["place"]) and "서울" not in e["place"]:
        return "지방 행사"
    first_host = e["hosts"].split(",")[0].strip()
    if REGIONAL_BRANCH.search(first_host):
        return "전국 조직의 지역본부"
    if not national and REGIONAL_HOST.search(first_host):
        return "지역 조직"
    if e.get("scope") == "지역" and re.search(REGIONS, e["hosts"] + " " + e["place"]) and "서울" not in e["place"]:
        return "지역 현안"
    hosts = [h.strip() for h in e["hosts"].split(",") if h.strip()]
    if hosts and all(is_party(h) for h in hosts):
        return "정당 주최"
    return None


DEDUPE_PROMPT = """아래는 같은 기간의 시민사회 기자회견·성명 목록입니다.
같은 행사(같은 기자회견, 같은 성명)를 가리키는 항목끼리 번호를 묶어 주세요.
JSON으로만 답하세요: {{"groups": [[번호, ...], ...]}}  (모든 번호를 한 번씩)

{items}"""


def dedupe_final(events):
    """정리된 결과에서 같은 행사 한 번 더 합치기 (목록이 짧아 정확함)"""
    lines = [f"{i}. [{e['type']}] {e['date']} {e['hosts']} | {e['title']} | {e['place']}"
             for i, e in enumerate(events, 1)]
    res = parse_json(call_claude(DEDUPE_PROMPT.format(items="\n".join(lines)),
                                 CONFIG.get("model_daily", "claude-haiku-5-5"), 2000, 120,
                                 thinking={"type": "disabled"}))
    used, out = set(), []
    for grp in res.get("groups", []):
        ids = [i for i in grp if isinstance(i, int) and 1 <= i <= len(events) and i not in used]
        if not ids:
            continue
        used.update(ids)
        members = [events[i - 1] for i in ids]
        best = max(members, key=lambda e: (len(e["sources"]), len(e["summary"])))
        if any(m.get("scope") == "전국" for m in members):
            best["scope"] = "전국"
        seen_urls = set()
        best["sources"] = [s for m in members for s in m["sources"]
                           if not (s["url"] in seen_urls or seen_urls.add(s["url"]))]
        for m in members:
            best["keywords"] += [k for k in m["keywords"] if k not in best["keywords"]]
        out.append(best)
    out += [e for i, e in enumerate(events, 1) if i not in used]
    log(f"최종 중복 합치기: {len(events)}건 → {len(out)}건")
    return out


def host_key(hosts):
    """주최 기관을 비교용으로 정리 (정식 명칭 → 약칭, 순서 무시)"""
    names = set()
    for h in hosts.split(","):
        h = h.strip()
        for o in CONFIG["orgs"]:
            if any(al in h for al in o["aliases"]):
                h = o["name"]
                break
        if h:
            names.add(re.sub(r"\s|\(.*?\)", "", h))
    return names


COMMON_WORDS = re.compile(r"촉구|요구|규탄|반대|발표|기자회견|성명|논평|입장|관련|및|대한|위한|국회|정부|즉각|철회|중단")


def topic_grams(title):
    """핵심 낱말의 두 글자 조각 (흔한 말 제외, 낱말 경계 유지)"""
    grams = set()
    for w in COMMON_WORDS.sub(" ", title).split():
        w = re.sub(r"[^가-힣A-Za-z0-9]", "", w)
        grams |= {w[i:i + 2] for i in range(len(w) - 1)}
    return grams


def merge_similar(events):
    """같은 날·같은 주최·비슷한 제목이면 한 건으로 (Claude 합치기의 빈틈 보완)"""
    out = []
    for e in events:
        for o in out:
            hk1, hk2 = host_key(e["hosts"]), host_key(o["hosts"])
            same_hosts = hk1 and hk2 and (hk1 & hk2) and len(hk1 & hk2) >= min(len(hk1), len(hk2)) / 2
            if e["date"] == o["date"] and same_hosts and len(topic_grams(e["title"]) & topic_grams(o["title"])) >= 2:
                keep, drop = (o, e) if len(o["sources"]) >= len(e["sources"]) else (e, o)
                if drop.get("scope") == "전국":
                    keep["scope"] = "전국"
                urls = {x["url"] for x in keep["sources"]}
                keep["sources"] += [x for x in drop["sources"] if x["url"] not in urls]
                if keep is e:
                    out[out.index(o)] = e
                break
        else:
            out.append(e)
    return out


def enrich(ev):
    main = ev["main"]
    text = article_text(main["url"]) or main["snippet"]
    info = extract_rules(ev, text)
    if has_claude():
        try:
            try:
                llm = extract_llm(ev, text)
            except ValueError:  # 응답 형식이 깨졌으면 한 번 더
                llm = extract_llm(ev, text)
            if llm.get("skip"):
                log(f"[관련 없음, 제외] {main['title']}")
                return None
            for k, v in llm.items():
                if k in info and v:
                    info[k] = as_text(v) if k == "summary" else squash(str(v))
            info["datetime"] = fix_date(info["datetime"], main["published"])
            if info["type"] not in ("기자회견", "집회"):
                info["place"] = ""
        except Exception as e:
            if ev["org"] == OTHER and has_claude():
                log(f"[Claude 정리 실패, 제외] {main['title']}: {e}")
                return None
            log(f"[Claude 정리 실패, 규칙 기반 사용] {main['title']}: {e}")
    # 행사일: Claude가 준 date → 일시 문구 → 기사 게시일 순으로 사용 (연도는 게시일 기준 보정)
    d = info.get("date", "")
    if main["source"] == "homepage" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
        log(f"[홈페이지 글 날짜 확인 불가, 제외] {main['title']}")
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
        d = fix_date(d.replace("-", "."), main["published"])
    else:
        d = info["datetime"]
    m = re.search(r"(\d{4})\.(\d{2})\.(\d{2})", d)
    info["date"] = f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else main["published"].date().isoformat()
    org = ev["org"]
    if org == OTHER:  # 지정 단체가 주최에 들어 있으면 가장 먼저 적힌 단체로
        pos = [(min(info["hosts"].find(a) for a in o["aliases"] if a in info["hosts"]), o["name"])
               for o in CONFIG["orgs"] if any(a in info["hosts"] for a in o["aliases"])]
        if pos:
            org = min(pos)[1]
    sources = [{"title": a["title"], "url": a["url"], "press": a["press"]} for a in ev["articles"]]
    return {**info, "org": org, "source": main["source"], "keywords": ev["keywords"],
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
            f"**주요 내용 :** {as_text(ev['summary'])}  ",
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
    # 일요일: 금~일, 그 외: 당일. 기간 안에 게시된 기사만 검색
    start_day = today - timedelta(days=2) if today.weekday() == 6 else today
    since = datetime.combine(start_day, time(0, 0), KST)
    period = period_label(start_day, today)
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
        searches = [(f"{o['name']} {kw}", o["name"], o["aliases"], kw)
                    for o in CONFIG["orgs"] for kw in keywords]
        for q in CONFIG.get("extra_queries", []):  # 지정 단체 외 주요 기자회견·성명
            kw = next((k for k in keywords if k in q), "기자회견")
            searches.append((q, OTHER, None, kw))
        for query, org_name, aliases, kw in searches:
            try:
                for it in search_naver(query, since, org_name, aliases, kw):
                    if it["published"] <= end:
                        add(it)
            except Exception as e:
                detail = ""
                if hasattr(e, "read"):  # 네이버가 보낸 상세 사유
                    try:
                        detail = " | " + e.read().decode("utf-8", "replace")[:300]
                    except Exception:
                        pass
                log(f"[네이버 실패] {query}: {e}{detail}")
                if getattr(e, "code", None) in (401, 403):
                    log("인증 오류라 나머지 네이버 검색을 건너뜁니다.")
                    break
    else:
        log("NAVER_CLIENT_ID/SECRET 없음: 네이버 검색을 건너뜁니다.")

    # 홈페이지 새 글은 모두 본문을 읽어 날짜로 거름 (페이지당 최근 20개)
    # 기록 형식: {주소: 처음 본 날짜}. 오늘 처음 본 글은 같은 날 다시 실행해도 다시 처리
    raw = load_json(SEEN_FILE) if SEEN_FILE.exists() else {}
    seen_dates = raw if isinstance(raw, dict) else {u: "0000-00-00" for u in raw}
    ds_today = today.isoformat()
    seen = {u for u, d in seen_dates.items() if d < ds_today}
    for org in CONFIG["orgs"]:
        for n, it in enumerate(scrape_homepage(org, keywords, seen, now)):
            seen_dates.setdefault(it["url"], ds_today)
            if n < 20:
                add(it)
    save_json(SEEN_FILE, dict(sorted(seen_dates.items())))

    groups = group_events(list(items.values()))
    groups.sort(key=lambda g: len(g["articles"]), reverse=True)
    # 긴 목록을 한 번에 묶으면 엉뚱하게 합쳐지므로, 사안별로 각각 정리한 뒤 마지막에 짧은 목록으로 합침
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=6) as pool:
        enriched = list(pool.map(enrich, groups[:CONFIG.get("max_events", 200)]))
    events, old_posts = [], []
    for e in enriched:
        if not e:
            continue
        if e["date"] < start_day.isoformat():
            old_posts.append(e)  # 이전 날짜 발표: 오늘 기사에 다시 나온 같은 발표를 거르는 데 씀
        if e["type"] not in TYPES + ["집회"]:  # 단순 보도는 제외
            continue
        if not (start_day.isoformat() <= e["date"] <= today.isoformat()):
            log(f"[기간 밖, 제외] {e['date']} {e['title']}")
            continue
        events.append(e)
    if has_claude() and len(events) > 1:
        try:
            events = dedupe_final(events)
        except Exception as ex:
            log(f"[최종 합치기 실패] {ex}")
    n_before = len(events)
    events = merge_similar(events)
    # 지역·정당 필터는 합친 뒤에 (같은 행사 기사 중 하나라도 '전국'이면 전국 이슈)
    kept = []
    for e in events:
        why = excluded(e)
        if why:
            log(f"[{why}, 제외] {e['hosts']} | {e['title']}")
        else:
            kept.append(e)
    events = kept
    # 4. 단체 홈페이지에서 이전 날짜로 확인된 성명·논평이 오늘 기사에 다시 나온 경우 제외
    #    (기자회견은 미리 예고 글이 올라오는 경우가 많아 제외 대상에서 뺌)
    old_posts = [o for o in old_posts if o.get("source") == "homepage" and o["type"] in ("성명", "논평")]
    kept = []
    for e in events:
        prev = next((o for o in old_posts
                     if e["type"] in ("성명", "논평") and host_key(e["hosts"]) & host_key(o["hosts"])
                     and len(topic_grams(e["title"]) & topic_grams(o["title"])) >= 5), None)
        if prev:
            log(f"[이전 발표({prev['date']}), 제외] {e['hosts']} | {e['title']}")
        else:
            kept.append(e)
    events = kept
    if len(events) < n_before:
        log(f"주최·제목 기준 추가 합치기: {n_before}건 → {len(events)}건")
    order = {t: i for i, t in enumerate(TYPES + ["집회"])}
    org_order = {o["name"]: i for i, o in enumerate(CONFIG["orgs"])}
    events.sort(key=lambda e: (order.get(e["type"], 9), org_order.get(e["org"], 99), e["published"]))

    ds = today.isoformat()
    day = {
        "kind": "daily", "date": ds,
        "period_start": start_day.isoformat(), "period_end": ds,
        "period_label": period,
        "generated_at": now.isoformat(timespec="minutes"),
        "count": len(events), "keywords": keywords,
        "orgs": [o["name"] for o in CONFIG["orgs"]] + [OTHER], "events": events,
        "files": {"docx": f"files/daily-{ds}.docx", "pdf": f"files/daily-{ds}.pdf"},
    }
    save_json(DATA_DIR / f"{ds}.json", day)
    build_index()
    (BUILD_DIR / "daily-notes.md").write_text(release_notes(day), encoding="utf-8")
    (BUILD_DIR / "daily-title.txt").write_text(f"시민사회 동향 {day['period_label']}", encoding="utf-8")
    log(f"완료: {day['period_label']} 기사 {len(items)}건 → 사안 {len(events)}건")


if __name__ == "__main__":
    main()
