#!/usr/bin/env python3
"""일일·주간 동향을 Word(.docx)와 PDF로 만듭니다.

사용: python scripts/render.py [YYYY-MM-DD]   (기본: 오늘)
PDF는 Chrome(헤드리스)으로 인쇄용 HTML을 변환합니다.
"""
import html
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from common import DATA_DIR, FILES_DIR, load_json, log, report_date

FONT = "맑은 고딕"
ACCENT = RGBColor(0x1F, 0x6F, 0x5C)
GREY = RGBColor(0x6B, 0x6B, 0x6B)
LETTERS = "가나다라마바사아"
e = html.escape


# ================================================================ Word
def new_doc():
    doc = Document()
    for s in doc.sections:
        s.page_height, s.page_width = Cm(29.7), Cm(21.0)
        s.top_margin = s.bottom_margin = Cm(2.0)
        s.left_margin = s.right_margin = Cm(2.2)
    for name in ("Normal", "Title", "Heading 1", "Heading 2", "Heading 3"):
        st = doc.styles[name]
        st.font.name = FONT
        rpr = st.element.get_or_add_rPr()
        fonts = rpr.find(qn("w:rFonts"))
        if fonts is None:
            fonts = OxmlElement("w:rFonts")
            rpr.append(fonts)
        for attr in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
            fonts.set(qn(attr), FONT)
    doc.styles["Normal"].font.size = Pt(10.5)
    doc.styles["Normal"].paragraph_format.space_after = Pt(3)
    doc.styles["Normal"].paragraph_format.line_spacing = 1.3
    for name, size in (("Title", 20), ("Heading 1", 14), ("Heading 2", 12), ("Heading 3", 11)):
        doc.styles[name].font.size = Pt(size)
        doc.styles[name].font.color.rgb = RGBColor(0x1D, 0x1D, 0x1F)
    return doc


def label_line(doc, label, value):
    p = doc.add_paragraph()
    r = p.add_run(f"{label} : ")
    r.bold = True
    p.add_run(value or "-")
    return p


def shade(cell, color):
    tcpr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), color)
    tcpr.append(shd)


def daily_docx(d, out):
    doc = new_doc()
    doc.add_heading("시민사회 동향", 0)
    p = doc.add_paragraph()
    r = p.add_run(f"{d['period_label']}  ·  총 {d['count']}건")
    r.font.color.rgb = GREY
    if not d["events"]:
        doc.add_paragraph("해당 기간에 수집된 소식이 없습니다.")
    for ev in d["events"]:
        h = doc.add_heading(level=2)
        t = h.add_run(f"[{ev['type']}] ")
        t.font.color.rgb = ACCENT
        h.add_run(ev["title"])
        if ev["place"]:
            label_line(doc, "일시 / 장소", f"{ev['datetime']} / {ev['place']}")
        else:
            label_line(doc, "일시", ev["datetime"])
        label_line(doc, "주최 기관", ev["hosts"])
        label_line(doc, "주요 내용", ev["summary"])
        src = doc.add_paragraph()
        first = ev["sources"][0]
        more = f" 외 {len(ev['sources']) - 1}건" if len(ev["sources"]) > 1 else ""
        r = src.add_run(f"출처: {first['press']}{more}  {first['url']}")
        r.font.size, r.font.color.rgb = Pt(8.5), GREY
    doc.save(out)


def weekly_docx(w, out):
    doc = new_doc()
    doc.add_heading("주간 시민사회 동향", 0)
    p = doc.add_paragraph()
    r = p.add_run(f"{w['period_label']}  ·  기자회견·성명·논평 등 {w['count']}건")
    r.font.color.rgb = GREY

    doc.add_heading("1. 개요", 1)
    for para in w["overview"].split("\n\n"):
        doc.add_paragraph(para.strip())
    doc.add_heading("2. 주요 정책/기조 현안", 1)
    for i, it in enumerate(w["main_issues"]):
        doc.add_heading(f"{LETTERS[i]}. {it['title']}", 3)
        doc.add_paragraph(it["body"])
    doc.add_heading("3. 그 외 주요 쟁점", 1)
    for i, it in enumerate(w["other_issues"]):
        doc.add_heading(f"{LETTERS[i]}. {it['title']}", 3)
        doc.add_paragraph(it["body"])
    if not w["other_issues"]:
        doc.add_paragraph("-")

    doc.add_heading("4. 참고자료", 1)
    table = doc.add_table(rows=1, cols=3)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    widths = (Cm(4.0), Cm(4.2), Cm(8.4))
    for cell, text, wd in zip(table.rows[0].cells, ("주요 쟁점", "단위주체", "핵심주장"), widths):
        cell.width = wd
        cell.text = ""
        run = cell.paragraphs[0].add_run(text)
        run.bold = True
        shade(cell, "E6EFEC")
    for row in w["table"]:
        cells = table.add_row().cells
        for cell, wd in zip(cells, widths):
            cell.width = wd
        cells[0].text = row["issue"]
        cells[1].text = row["actor"]
        if row.get("meta"):
            m = cells[1].add_paragraph().add_run(row["meta"])
            m.font.size, m.font.color.rgb = Pt(8), GREY
        cells[2].text = row["claim"]
    for r_ in table.rows:
        for c in r_.cells:
            for para in c.paragraphs:
                para.paragraph_format.space_after = Pt(0)
                for run in para.runs:
                    if run.font.size is None:
                        run.font.size = Pt(9.5)
    doc.save(out)


# ================================================================ PDF (인쇄용 HTML → Chrome)
CSS = """
@page { size: A4; margin: 18mm 20mm; }
body { font-family: "Malgun Gothic", "Noto Sans CJK KR", "Noto Sans KR", sans-serif; font-size: 10.5pt; line-height: 1.6; color: #1d1d1f; }
h1 { font-size: 20pt; margin: 0 0 2pt; }
.period { color: #6b6b6b; margin-bottom: 14pt; }
h2 { font-size: 13.5pt; border-bottom: 1.5px solid #1f6f5c; padding-bottom: 3pt; margin: 16pt 0 6pt; }
h3 { font-size: 11pt; margin: 10pt 0 3pt; }
.ev { border-top: 1px solid #ddd; padding: 8pt 0 6pt; page-break-inside: avoid; }
.ev h3 { margin: 0 0 4pt; }
.type { color: #1f6f5c; }
.row { margin: 1pt 0; } .row b { display: inline-block; min-width: 5.4em; }
.src { font-size: 8.5pt; color: #6b6b6b; word-break: break-all; }
p { margin: 0 0 6pt; text-align: justify; }
table { width: 100%; border-collapse: collapse; font-size: 9pt; }
th, td { border: 1px solid #bbb; padding: 4pt 5pt; vertical-align: top; text-align: left; }
th { background: #e6efec; } tr { page-break-inside: avoid; }
td small { display: block; color: #6b6b6b; font-size: 7.5pt; }
"""


def page(title, body):
    return f'<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8"><title>{e(title)}</title><style>{CSS}</style></head><body>{body}</body></html>'


def daily_html(d):
    parts = [f"<h1>시민사회 동향</h1><div class='period'>{e(d['period_label'])} · 총 {d['count']}건</div>"]
    if not d["events"]:
        parts.append("<p>해당 기간에 수집된 소식이 없습니다.</p>")
    for ev in d["events"]:
        when = ev["datetime"] + (f" / {ev['place']}" if ev["place"] else "")
        first = ev["sources"][0]
        more = f" 외 {len(ev['sources']) - 1}건" if len(ev["sources"]) > 1 else ""
        parts.append(
            f"<div class='ev'><h3><span class='type'>[{e(ev['type'])}]</span> {e(ev['title'])}</h3>"
            f"<div class='row'><b>{'일시 / 장소' if ev['place'] else '일시'}</b> : {e(when)}</div>"
            f"<div class='row'><b>주최 기관</b> : {e(ev['hosts'])}</div>"
            f"<div class='row'><b>주요 내용</b> : {e(ev['summary'])}</div>"
            f"<div class='src'>출처: {e(first['press'])}{more} · {e(first['url'])}</div></div>")
    return page(f"시민사회 동향 {d['period_label']}", "".join(parts))


def weekly_html(w):
    def issues(items):
        return "".join(f"<h3>{LETTERS[i]}. {e(it['title'])}</h3><p>{e(it['body'])}</p>"
                       for i, it in enumerate(items)) or "<p>-</p>"
    rows = "".join(
        f"<tr><td>{e(r['issue'])}</td><td>{e(r['actor'])}<small>{e(r.get('meta', ''))}</small></td>"
        f"<td>{e(r['claim'])}</td></tr>" for r in w["table"])
    body = (f"<h1>주간 시민사회 동향</h1><div class='period'>{e(w['period_label'])} · 기자회견·성명·논평 등 {w['count']}건</div>"
            "<h2>1. 개요</h2>" + "".join(f"<p>{e(p.strip())}</p>" for p in w["overview"].split("\n\n"))
            + "<h2>2. 주요 정책/기조 현안</h2>" + issues(w["main_issues"])
            + "<h2>3. 그 외 주요 쟁점</h2>" + issues(w["other_issues"])
            + "<h2>4. 참고자료</h2><table><colgroup><col style='width:24%'><col style='width:24%'><col></colgroup>"
            + f"<tr><th>주요 쟁점</th><th>단위주체</th><th>핵심주장</th></tr>{rows}</table>")
    return page(f"주간 시민사회 동향 {w['period_label']}", body)


def find_chrome():
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        if shutil.which(name):
            return shutil.which(name)
    raise RuntimeError("Chrome을 찾지 못했습니다")


def to_pdf(html_text, out):
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "page.html"
        src.write_text(html_text, encoding="utf-8")
        subprocess.run([find_chrome(), "--headless=new", "--no-sandbox", "--disable-gpu",
                        "--no-pdf-header-footer", f"--print-to-pdf={out}", src.as_uri()],
                       check=True, capture_output=True, timeout=180)


# ================================================================ 실행
def main():
    ds = sys.argv[1] if len(sys.argv) > 1 else report_date().isoformat()
    jobs = [("daily", daily_docx, daily_html), ("weekly", weekly_docx, weekly_html)]
    for kind, make_docx, make_html in jobs:
        src = DATA_DIR / (f"{ds}.json" if kind == "daily" else f"weekly-{ds}.json")
        if not src.exists():
            continue
        data = load_json(src)
        make_docx(data, FILES_DIR / f"{kind}-{ds}.docx")
        to_pdf(make_html(data), FILES_DIR / f"{kind}-{ds}.pdf")
        log(f"파일 생성: {kind}-{ds}.docx / .pdf")


if __name__ == "__main__":
    main()
