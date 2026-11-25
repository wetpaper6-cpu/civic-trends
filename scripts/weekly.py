#!/usr/bin/env python3
"""주간 시민사회 동향 (목요일): 직전 금요일 ~ 당일 목요일 7일간

양식
 1) 개요            : 전반적 내용 요약 (반 페이지)
 2) 주요 정책/기조 현안 : 가장 이슈가 된 현안 위주 (한 페이지 이내)
 3) 그 외 주요 쟁점    : (반 페이지)
 4) 참고자료         : 기자회견·성명 등 표 (주요 쟁점 / 단위주체 / 핵심주장)

1)~3)은 Claude가 작성합니다(ANTHROPIC_API_KEY 필요). 키가 없으면 통계 위주로 대신 채웁니다.
"""
from collections import Counter
from datetime import timedelta

from common import (BUILD_DIR, CONFIG, DATA_DIR, call_claude, has_claude, load_json, log,
                    now_kst, parse_json, period_label, report_date, save_json, build_index)

PROMPT = """당신은 한국 시민사회 동향을 정리하는 정책 분석가입니다.
아래는 {period} 동안 발표된 시민사회 기자회견·성명·논평 목록입니다.
이 목록만을 근거로 'A4 두 페이지' 분량의 주간 시민사회 동향 보고서를 **개조식**으로 작성하세요.

JSON으로만 답하세요:
{{
 "overview": ["1) 개요: 한 주의 전반적 흐름. 항목 5~7개"],
 "main_issues": [{{"title": "현안명", "points": ["배경, 단체별 입장·요구, 대립 지점. 항목 4~6개"]}}],
 "other_issues": [{{"title": "쟁점명", "points": ["항목 2~3개"]}}],
 "table": [{{"issue": "주요 쟁점(짧게)", "actor": "단위주체(단체명)", "claim": "핵심주장 한 문장", "ref": [항목 번호]}}]
}}

개조식 작성 기준
- 각 항목은 한두 줄의 짧은 구절로, 명사형으로 끝맺기 (예: "~ 촉구", "~ 요구함", "~ 비판", "~ 예정임")
- "~했다", "~이다" 같은 서술형 문장 금지. 항목 앞에 기호(○, -, 숫자)는 붙이지 말 것
- 주체(단체명)를 항목 앞부분에 밝히기 (예: "참여연대, 기초연금 개편안 국회 심의 중단 요구")
- main_issues: 이번 주 가장 크게 이슈가 된 정책·기조 현안 2~3개 (한 페이지 이내)
- other_issues: 그 밖의 주요 쟁점 2~4개 (반 페이지)
- overview: 반 페이지
- table: 목록의 모든 항목을 빠짐없이 반영. 같은 단체가 같은 쟁점으로 낸 항목은 한 줄로 합치고 ref에 번호를 모두 적기. 같은 쟁점끼리 붙여서 정렬
- 목록에 없는 사실·수치·평가를 지어내지 말 것

목록
{items}"""


def as_list(v):
    """개조식 항목 목록 (예전 형식의 문단 문자열도 처리)"""
    if isinstance(v, list):
        return [str(x).strip().lstrip("○-·•").strip() for x in v if str(x).strip()]
    return [p.strip() for p in str(v or "").split("\n") if p.strip()]


def points(item):
    return as_list(item.get("points", item.get("body", "")))


def main():
    today = report_date()
    start = today - timedelta(days=6)  # 직전 금요일
    events, seen = [], set()
    for p in sorted(DATA_DIR.glob("????-??-??.json")):
        d = load_json(p)
        if not (start.isoformat() <= d["period_end"] <= today.isoformat()):
            continue
        for ev in d["events"]:
            if ev["url"] in seen:
                continue
            seen.add(ev["url"])
            events.append(ev)
    events.sort(key=lambda e: e["published"])
    label = period_label(start, today)
    log(f"주간: {label} 사안 {len(events)}건")

    lines = [f"{i}. [{e['type']}] {e['published'][5:10]} {e['hosts']} | {e['title']} | {e['summary']}"
             for i, e in enumerate(events, 1)]

    report = None
    if has_claude() and events:
        try:
            report = parse_json(call_claude(
                PROMPT.format(period=label, orgs=", ".join(o["name"] for o in CONFIG["orgs"]),
                              items="\n".join(lines)),
                CONFIG.get("model_weekly", "claude-sonnet-5-5"), 32000, 900))  # 생각 + 보고서
        except Exception as e:
            log(f"[Claude 주간 작성 실패, 기본 양식 사용] {e}")

    if report is None:  # 키가 없거나 실패: 통계 + 원자료로 채움
        by_type = Counter(e["type"] for e in events)
        by_org = Counter(e["org"] for e in events)
        report = {
            "overview": ([f"{label} 수집된 시민사회 동향 총 {len(events)}건",
                          "유형별: " + ", ".join(f"{k} {v}건" for k, v in by_type.most_common()),
                          "단체별: " + ", ".join(f"{k} {v}건" for k, v in by_org.most_common())]
                         if events else [f"{label} 수집된 소식 없음"]),
            "main_issues": [{"title": "(자동 요약 미사용)",
                             "points": ["ANTHROPIC_API_KEY를 등록하면 이 부분을 Claude가 작성함"]}],
            "other_issues": [],
            "table": [{"issue": e["title"], "actor": e["hosts"], "claim": e["summary"],
                       "ref": [i]} for i, e in enumerate(events, 1)],
        }

    # 표에 유형·날짜 붙이기
    for row in report.get("table", []):
        refs = [r for r in row.get("ref", []) if isinstance(r, int) and 1 <= r <= len(events)]
        row["meta"] = ", ".join(sorted({f"{events[r-1]['type']} {events[r-1]['published'][5:10].replace('-', '.')}"
                                        for r in refs}))
        row["urls"] = [events[r - 1]["url"] for r in refs]

    ds = today.isoformat()
    weekly = {
        "kind": "weekly", "date": ds,
        "period_start": start.isoformat(), "period_end": ds, "period_label": label,
        "generated_at": now_kst().isoformat(timespec="minutes"),
        "count": len(events), **report,
        "files": {"docx": f"files/weekly-{ds}.docx", "pdf": f"files/weekly-{ds}.pdf"},
    }
    save_json(DATA_DIR / f"weekly-{ds}.json", weekly)
    build_index()

    md = [f"# 주간 시민사회 동향 · {label}", "",
          "📄 Word·PDF 파일은 맨 아래 **Assets**에서 내려받을 수 있습니다.", "",
          "## 1. 개요", *[f"- {p}" for p in as_list(weekly["overview"])], "", "## 2. 주요 정책/기조 현안"]
    for i, it in enumerate(weekly["main_issues"]):
        md += [f"**{'가나다라마바'[i]}. {it['title']}**", *[f"- {p}" for p in points(it)], ""]
    md.append("## 3. 그 외 주요 쟁점")
    for i, it in enumerate(weekly["other_issues"]):
        md += [f"**{'가나다라마바'[i]}. {it['title']}**", *[f"- {p}" for p in points(it)], ""]
    md += ["## 4. 참고자료", "", "| 주요 쟁점 | 단위주체 | 핵심주장 |", "|---|---|---|"]
    for r in weekly["table"]:
        cell = lambda s: str(s).replace("|", "/").replace("\n", " ")
        md.append(f"| {cell(r['issue'])} | {cell(r['actor'])} <sub>{r['meta']}</sub> | {cell(r['claim'])} |")
    (BUILD_DIR / "weekly-notes.md").write_text("\n".join(md), encoding="utf-8")
    (BUILD_DIR / "weekly-title.txt").write_text(f"주간 시민사회 동향 {label}", encoding="utf-8")


if __name__ == "__main__":
    main()
