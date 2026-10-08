"""Equity-report PDF of one stored run — built from the run log, no model calls.

WHY ReportLab: pure Python, no system libraries, so it works in the Railway
container as-is and lays out a 15-page report in well under a second.

Shape of the report (what a research reader expects, in that order):
  1. title block + key metrics (from the evidence packet's market section)
  2. the CALL first: the governed verdict, with each UNGROUNDED tag turned into
     a numbered footnote marker and the footnotes beneath
  3. the debate, round by round: bull case, bear case (numbered points as
     sub-headings), the judge's strongest point per side, flagged claims
  4. claim review, review statistics
  5. appendix: filings & news evidence, methodology, disclaimer
Fonts: IBM Plex Serif for body (reads as a report), JetBrains Mono for
numbers, labels and evidence (ties it to the site). Both are OFL and
embedded from markut/web/static/fonts.
"""
import io
import os
import re
import time
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (CondPageBreak, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)

FONT_DIR = os.path.join(os.path.dirname(__file__), "web", "static", "fonts")
_FONTS = {"Serif": "IBMPlexSerif-Regular.ttf", "Serif-Bold": "IBMPlexSerif-Bold.ttf",
          "Serif-Italic": "IBMPlexSerif-Italic.ttf", "Serif-BoldItalic": "IBMPlexSerif-BoldItalic.ttf",
          "Mono": "JetBrainsMono-Regular.ttf", "Mono-Bold": "JetBrainsMono-Bold.ttf",
          "Mono-Italic": "JetBrainsMono-Italic.ttf"}
_registered = False

INK = colors.HexColor("#1b1b1f")
MUTED = colors.HexColor("#6b6b73")
LINE = colors.HexColor("#d9d7d0")
BULL = colors.HexColor("#1f8a4c")
BEAR = colors.HexColor("#c0392b")
JUDGE = colors.HexColor("#6b4fbb")
REVIEW = colors.HexColor("#8a6508")
PANEL = colors.HexColor("#f4f3ef")
TAG = colors.HexColor("#8e1b12")

UNGROUNDED = "[UNGROUNDED — no evidence anchor]"
_PRICE_RE = re.compile(r"Price \(current\):\s*(\$[\d,.]+)")
_NUM_RE = re.compile(r"(\$[\d,]+(?:\.\d+)?\s?(?:[BMTK]|bn|billion|million|trillion)?|\d+(?:\.\d+)?\s?%|\d+(?:\.\d+)?x\b"
                     r"|\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b|\b\d+\.\d+\b|\b(?:FY|Q[1-4]\s?FY?)\d{2,4}\b)")


def _fonts():
    global _registered
    if _registered:
        return
    for name, fname in _FONTS.items():
        pdfmetrics.registerFont(TTFont(name, os.path.join(FONT_DIR, fname)))
    pdfmetrics.registerFontFamily("Serif", normal="Serif", bold="Serif-Bold", italic="Serif-Italic", boldItalic="Serif-BoldItalic")
    pdfmetrics.registerFontFamily("Mono", normal="Mono", bold="Mono-Bold", italic="Mono-Italic", boldItalic="Mono-Bold")
    _registered = True


def _styles():
    _fonts()
    base = dict(fontName="Serif", fontSize=10.2, leading=15, textColor=INK, alignment=TA_LEFT)
    s = {
        "body": ParagraphStyle("body", spaceAfter=7, **base),
        "small": ParagraphStyle("small", fontName="Serif", fontSize=8.6, leading=12, textColor=MUTED, spaceAfter=4),
        "label": ParagraphStyle("label", fontName="Mono", fontSize=7.6, leading=10, textColor=MUTED, spaceAfter=2),
        "ticker": ParagraphStyle("ticker", fontName="Mono-Bold", fontSize=30, leading=34, textColor=INK, spaceAfter=2),
        "title": ParagraphStyle("title", fontName="Serif-Bold", fontSize=16, leading=20, textColor=INK, spaceAfter=4),
        "h1": ParagraphStyle("h1", fontName="Serif-Bold", fontSize=14, leading=18, textColor=INK, spaceBefore=14, spaceAfter=6),
        "h2": ParagraphStyle("h2", fontName="Serif-Bold", fontSize=11.2, leading=15, textColor=INK, spaceBefore=9, spaceAfter=3),
        "h2bull": ParagraphStyle("h2bull", fontName="Serif-Bold", fontSize=11.2, leading=15, textColor=BULL, spaceBefore=9, spaceAfter=3),
        "h2bear": ParagraphStyle("h2bear", fontName="Serif-Bold", fontSize=11.2, leading=15, textColor=BEAR, spaceBefore=9, spaceAfter=3),
        "pt": ParagraphStyle("pt", fontName="Serif-Bold", fontSize=10.2, leading=14, textColor=INK, spaceBefore=6, spaceAfter=2),
        "bullet": ParagraphStyle("bullet", leftIndent=12, bulletIndent=2, spaceAfter=3, **base),
        "mono": ParagraphStyle("mono", fontName="Mono", fontSize=7.4, leading=10, textColor=INK),
        "foot": ParagraphStyle("foot", fontName="Serif", fontSize=8.6, leading=12, textColor=MUTED, leftIndent=12, firstLineIndent=-12, spaceAfter=2),
        "verdict": ParagraphStyle("verdict", fontName="Serif", fontSize=10.8, leading=16.5, textColor=INK, spaceAfter=8),
        "box": ParagraphStyle("box", fontName="Serif", fontSize=9.4, leading=13.5, textColor=INK),
        "boxlabel": ParagraphStyle("boxlabel", fontName="Mono-Bold", fontSize=7.4, leading=10, textColor=MUTED, spaceAfter=2),
    }
    return s


# ---------------------------------------------------------------- text shaping
def _inline(text: str, strip_bold: bool = True) -> str:
    """Markdown inline -> ReportLab paragraph markup. Numbers set in mono so the
    evidence stands out the same way it does on the site."""
    t = escape(str(text or ""))
    t = re.sub(r"`([^`]+)`", r'<font name="Mono" size="8.6">\1</font>', t)
    t = re.sub(r"\*\*(.+?)\*\*", r"\1" if strip_bold else r"<b>\1</b>", t)
    t = t.replace("**", "")   # a reply cut off mid-bold leaves a stray marker; never print it
    t = re.sub(r"(^|\W)\*(?!\s)(.+?)\*(?=\W|$)", r"\1<i>\2</i>", t)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r'<a href="\2" color="#2f5bea">\1</a>', t)
    # numbers in mono: operate only on text outside tags
    parts = re.split(r"(<[^>]+>)", t)
    t = "".join(p if p.startswith("<") else _NUM_RE.sub(r'<font name="Mono" size="9">\1</font>', p) for p in parts)
    return t


def split_argument(text: str) -> dict:
    """Same contract as debate.js splitArgument: '# title', then '## N.' points."""
    title, intro, sections, cur = "", [], [], None
    for raw in str(text or "").split("\n"):
        line = raw.rstrip()
        if re.match(r"^(-{3,}|\*{3,})$", line.strip()):
            continue
        t = re.match(r"^#\s+(.*)$", line)
        if t and not title and not sections:
            title = t.group(1).replace("**", "")
            continue
        h = re.match(r"^#{1,6}\s+(.*)$", line) or re.match(r"^\*\*(\d+[.)]\s[^*]{3,})\*\*\s*$", line)
        if h:
            cur = {"heading": h.group(1).replace("**", "").strip(), "body": []}
            sections.append(cur)
            continue
        (cur["body"] if cur else intro).append(line)
    return {"title": title, "intro": "\n".join(intro).strip(),
            "sections": [{"heading": s["heading"], "body": "\n".join(s["body"]).strip()} for s in sections]}


def _md_flow(text: str, st: dict) -> list:
    """Markdown body -> flowables (paragraphs, bullets, sub-headings)."""
    out, para, in_code = [], [], False
    def flush():
        if para:
            out.append(Paragraph(_inline(" ".join(para)), st["body"]))
            para.clear()
    for raw in str(text or "").split("\n"):
        line = raw.rstrip()
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            out.append(Paragraph(escape(line) or " ", st["mono"]))
            continue
        if not line.strip() or re.match(r"^(-{3,}|\*{3,})$", line.strip()):
            flush()
            continue
        m = re.match(r"^#{1,6}\s+(.*)$", line)
        if m:
            flush(); out.append(Paragraph(_inline(m.group(1)), st["pt"])); continue
        m = re.match(r"^\s*(?:[-*•]|\d+[.)])\s+(.*)$", line)
        if m:
            flush(); out.append(Paragraph(_inline(m.group(1)), st["bullet"], bulletText="•")); continue
        para.append(line)
    flush()
    return out


def parse_packet(evidence: str) -> tuple:
    """Market section of the packet -> [(section, [(key, value, source)])], plus the rest."""
    lines = str(evidence or "").split("\n")
    grid, section, i = [], None, 0
    for i, raw in enumerate(lines):
        l = raw.strip()
        if not l:
            continue
        m = re.match(r"^\[([^\]]+)\]$", l)
        if m:
            section = {"name": m.group(1), "rows": []}; grid.append(section); continue
        m = re.match(r"^-\s+([^:]+):\s+(.*?)\s*(?:\[source:\s*([^\]]+)\])?$", l)
        if m and section:
            section["rows"].append((m.group(1), m.group(2), m.group(3) or "")); continue
        break
    else:
        i = len(lines)
    rest = "\n".join(lines[i:]).strip() if i < len(lines) else ""
    return [s for s in grid if s["rows"]], rest


def footnote_verdict(verdict: str) -> tuple:
    """Replace each '<number> [UNGROUNDED — …]' with a superscript marker; return
    (markup, [numbers]) so the footnotes can be listed beneath."""
    tags = []
    def sub(m):
        tags.append(m.group(1))
        return f"{m.group(1)}<super><font name='Mono' size='7' color='#8e1b12'>[{len(tags)}]</font></super>"
    marked = re.sub(r"(\S+)\s\[UNGROUNDED — no evidence anchor\]", sub, str(verdict or ""))
    paras = [p for p in re.split(r"\n{2,}", marked) if p.strip()]
    return paras, tags


# ---------------------------------------------------------------- the document
def build_report(run: dict) -> bytes:
    st = _styles()
    events = run.get("events", []) or []
    by = {}
    for e in events:
        by.setdefault(e.get("event"), []).append(e.get("data", {}) or {})
    start = (by.get("start") or [{}])[0]
    research = (by.get("research") or [{}])[0]
    review = (by.get("review") or [{}])[0]
    done = (by.get("done") or [{}])[0]
    ticker = run.get("ticker") or start.get("ticker") or "?"
    ran_on = (run.get("started_at") or "")[:16]
    price = run.get("price") or (lambda m: m.group(1) if m else "")(_PRICE_RE.search(research.get("evidence", "")))
    model = run.get("model") or start.get("model") or ""
    header = f"markut.dev  ·  {ticker}  ·  {ran_on}"

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.95 * inch, rightMargin=0.95 * inch,
                            topMargin=0.9 * inch, bottomMargin=0.85 * inch,
                            title=f"Markut research debate — {ticker} — {ran_on}", author="Markut",
                            subject="Bull-vs-bear research debate with a governed verdict")

    def on_page(canvas, d):
        canvas.saveState()
        canvas.setFont("Mono", 7.2); canvas.setFillColor(MUTED)
        canvas.drawString(doc.leftMargin, letter[1] - 0.55 * inch, header)
        canvas.drawRightString(letter[0] - doc.rightMargin, letter[1] - 0.55 * inch, "RESEARCH DEBATE REPORT")
        canvas.setStrokeColor(LINE); canvas.setLineWidth(0.5)
        canvas.line(doc.leftMargin, letter[1] - 0.62 * inch, letter[0] - doc.rightMargin, letter[1] - 0.62 * inch)
        canvas.drawString(doc.leftMargin, 0.5 * inch, "Research framing only. Not investment advice.")
        canvas.drawRightString(letter[0] - doc.rightMargin, 0.5 * inch, f"page {d.page}")
        canvas.restoreState()

    width = letter[0] - doc.leftMargin - doc.rightMargin
    story = []

    # ---- 1. title block ----
    story += [Paragraph("MARKUT RESEARCH  ·  BULL-VS-BEAR DEBATE", st["label"]),
              Paragraph(escape(ticker), st["ticker"]),
              Paragraph("Multi-agent research debate with a governed verdict", st["title"])]
    meta = [("run date", ran_on or "—"), ("price seen", price or "—"), ("rounds", str(done.get("rounds", run.get("rounds", "—")))),
            ("converged", "yes" if (done.get("converged") if done else run.get("converged")) else "no"),
            ("model", model or "—"), ("run id", f"#{run.get('id', '—')}")]
    mw = [1.35, 1.0, 0.7, 0.9, 1.7, 0.7]
    mt = Table([[Paragraph(k.upper(), st["label"]) for k, _ in meta],
                [Paragraph(f"<font name='Mono' size='8.6'>{escape(v)}</font>", st["box"]) for _, v in meta]],
               colWidths=[width * w / sum(mw) for w in mw])
    mt.setStyle(TableStyle([("LINEBELOW", (0, 1), (-1, 1), 0.5, LINE), ("BOTTOMPADDING", (0, 1), (-1, 1), 6),
                            ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story += [Spacer(1, 6), mt, Spacer(1, 10)]

    # ---- key metrics ----
    grid, rest_evidence = parse_packet(research.get("evidence", ""))
    if grid:
        story.append(Paragraph("Key metrics", st["h1"]))
        story.append(Paragraph("From the evidence packet every agent saw; sources per line in the appendix.", st["small"]))
        rows = []
        for sec in grid:
            rows.append([Paragraph(escape(sec["name"].lower()), st["boxlabel"]), "", "", ""])
            pairs = sec["rows"]
            for j in range(0, len(pairs), 2):
                left = pairs[j]; right = pairs[j + 1] if j + 1 < len(pairs) else ("", "", "")
                rows.append([Paragraph(escape(left[0]), st["small"]), Paragraph(_inline(left[1]), st["box"]),
                             Paragraph(escape(right[0]), st["small"]), Paragraph(_inline(right[1]), st["box"])])
        kt = Table(rows, colWidths=[width * .27, width * .23, width * .27, width * .23])
        kt.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 2),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 2), ("TOPPADDING", (0, 0), (-1, -1), 2),
                                ("LINEBELOW", (0, 0), (-1, -1), 0.25, LINE)]))
        story += [kt, Spacer(1, 6)]

    # ---- 2. the call: governed verdict with footnoted tags ----
    verdict = review.get("verdict") or run.get("final_verdict") or ""
    stats = review.get("stats", {}) or {}
    story.append(Paragraph("Conclusion — governed verdict", st["h1"]))
    story.append(Paragraph("The judge's final verdict after the review governor traced every number to the evidence packet. "
                           "Numbers with no anchor are footnoted, not removed.", st["small"]))
    paras, tags = footnote_verdict(verdict)
    for p in paras:
        story.append(Paragraph(_inline(p, strip_bold=False).replace("&lt;super&gt;", "<super>").replace("&lt;/super&gt;", "</super>")
                               .replace("&lt;font name='Mono' size='7' color='#8e1b12'&gt;", "<font name='Mono' size='7' color='#8e1b12'>").replace("&lt;/font&gt;", "</font>"),
                               st["verdict"]))
    if tags:
        for i, t in enumerate(tags, 1):
            story.append(Paragraph(f"[{i}]  <font name='Mono'>{escape(t)}</font> — no anchor in the evidence packet; left tagged by the governor.", st["foot"]))
    else:
        story.append(Paragraph("Every number in the verdict traced to the evidence packet.", st["foot"]))
    if stats:
        srow = [("claims", stats.get("claims")), ("cited", stats.get("cited")), ("flagged", stats.get("flagged")),
                ("derived", stats.get("derived")), ("labeled", stats.get("labeled")), ("annotated", stats.get("annotated")), ("status", stats.get("status"))]
        sw = [1, 1, 1, 1, 1, 1, 1.6]
        stt = Table([[Paragraph(k.upper(), st["label"]) for k, _ in srow], [Paragraph(f"<font name='Mono-Bold' size='10'>{escape(str(v if v is not None else '—'))}</font>", st["box"]) for _, v in srow]],
                    colWidths=[width * w / sum(sw) for w in sw])
        stt.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), PANEL), ("LEFTPADDING", (0, 0), (-1, -1), 6),
                                 ("TOPPADDING", (0, 0), (-1, 0), 6), ("BOTTOMPADDING", (0, 1), (-1, 1), 6)]))
        story += [Spacer(1, 6), stt]

    # ---- 3. the debate ----
    story += [CondPageBreak(3.2 * inch), Paragraph("The debate", st["h1"]),
              Paragraph("Bull and bear analysts argue over the same evidence packet; from round two each rebuts the other. "
                        "The judge rules on each round and decides whether the debate has converged.", st["small"])]
    round_no = 0
    for e in events:
        name, d = e.get("event"), e.get("data", {}) or {}
        if name in ("bull", "bear"):
            r = d.get("round") or round_no or 1
            if r != round_no:
                round_no = r
                story.append(Paragraph(f"Round {r}", st["h1"]))
            arg = split_argument(d.get("text", ""))
            side = "Bull case" if name == "bull" else "Bear case"
            head = escape(arg["title"]) if arg["title"] and side.lower() in arg["title"].lower() else (side + (f" — {escape(arg['title'])}" if arg["title"] else ""))
            story.append(Paragraph(head, st["h2bull" if name == "bull" else "h2bear"]))
            if arg["intro"]:
                story += _md_flow(arg["intro"], st)
            if arg["sections"]:
                for i, sec in enumerate(arg["sections"], 1):
                    head = re.sub(r"^\d+[.)]\s*", "", sec["heading"])
                    story.append(Paragraph(f"{i}. {_inline(head)}", st["pt"]))
                    story += _md_flow(sec["body"], st)
            else:
                story += _md_flow(d.get("text", ""), st)
        elif name == "judge":
            if d.get("recorded") is False:
                story.append(Paragraph(escape(d.get("note", "")), st["small"]))
                continue
            cells = [[Paragraph("STRONGEST BULL POINT", st["boxlabel"]), Paragraph("STRONGEST BEAR POINT", st["boxlabel"])],
                     [Paragraph(_inline(d.get("bull_strongest") or "—"), st["box"]), Paragraph(_inline(d.get("bear_strongest") or "—"), st["box"])]]
            jt = Table(cells, colWidths=[width / 2 - 4, width / 2 - 4], hAlign="LEFT")
            jt.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), PANEL), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                    ("LINEABOVE", (0, 0), (0, 0), 1.2, BULL), ("LINEABOVE", (1, 0), (1, 0), 1.2, BEAR),
                                    ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                                    ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
            block = [Paragraph(f"Judge — round {d.get('round', round_no)} · {'converged' if d.get('converged') else 'not converged'}", st["h2"]), jt]
            claims = d.get("unsupported_claims") or []
            if claims:
                block.append(Paragraph(f"Flagged as unsupported ({len(claims)})", st["pt"]))
                block += [Paragraph(_inline(str(c)), st["bullet"], bulletText="•") for c in claims]
            if d.get("reasoning"):
                block += [Paragraph("Reasoning", st["pt"]), Paragraph(_inline(d["reasoning"]), st["body"])]
            story.append(KeepTogether(block[:2]))
            story += block[2:]
        elif name == "route":
            story.append(Paragraph(("↻ " if d.get("decision") == "continue" else "→ ") + escape(d.get("reason", "")), st["small"]))
        elif name == "news_verify":
            story.append(Paragraph("Claim review", st["h2"]))
            reviews = d.get("claim_reviews") or []
            if reviews:
                for r in reviews:
                    story.append(Paragraph(f"<font name='Mono' size='8'>[{escape(r.get('status', '?'))}]</font> {_inline(r.get('claim', ''))}"
                                           + (f" — {_inline(r.get('evidence_summary', ''))}" if r.get("evidence_summary") else ""), st["bullet"], bulletText="•"))
            else:
                story.append(Paragraph(_inline(d.get("reasoning") or "No unsupported claims to re-audit."), st["body"]))
            if d.get("verdict_changed"):
                story.append(Paragraph("The re-audit revised the verdict.", st["small"]))

    # ---- 4. usage ----
    u = done.get("usage") or {}
    if u:
        cached = (u.get("cache_read") or 0) + (u.get("cache_write") or 0)
        story.append(Paragraph(f"Run cost: {u.get('calls', '—')} model calls · {(u.get('input') or 0) + cached:,} input tokens"
                               + (f" ({u.get('cache_read') or 0:,} read from cache)" if cached else "")
                               + f" · {u.get('output', 0):,} output tokens.", st["small"]))

    # ---- 5. appendix ----
    story += [PageBreak(), Paragraph("Appendix A — evidence packet", st["h1"]),
              Paragraph("The immutable packet assembled by the research agent through three MCP tools (market data, SEC filings RAG, news). "
                        "This is the only material the analysts, judge and governor were allowed to cite.", st["small"])]
    for sec in grid:
        story.append(Paragraph(escape(sec["name"].lower()), st["boxlabel"]))
        for k, v, src in sec["rows"]:
            story.append(Paragraph(f"{escape(k)}: {escape(v)}" + (f"   <font color='#6b6b73'>[{escape(src)}]</font>" if src else ""), st["mono"]))
        story.append(Spacer(1, 4))
    if rest_evidence:
        story.append(Paragraph("filings &amp; news", st["boxlabel"]))
        for line in rest_evidence.split("\n"):
            story.append(Paragraph(escape(line) or " ", st["mono"]))
    story += [Paragraph("Appendix B — method", st["h1"]),
              Paragraph("Markut is a LangGraph state machine: Research → Bull ⇄ Bear → Judge (per round) → claim review → review governor. "
                        "The research agent discovers three evidence tools over MCP and assembles one packet per debate. Bull and bear "
                        "argue in rounds and rebut each other from round two. The judge returns strict JSON — strongest point per side, "
                        "unsupported claims, convergence. The claim review re-audits flagged claims against the packet only; news leads "
                        "are never evidence. The governor is deterministic: it extracts every numeric claim in the verdict, traces it to "
                        "the packet, allows one bounded model revision, and tags whatever still has no anchor instead of deleting it. "
                        "A token budget closes the debate through the same governed exit if a run balloons.", st["body"]),
              Paragraph(f"Generated {time.strftime('%Y-%m-%d %H:%M')} from stored run #{run.get('id', '—')} ({escape(run.get('mode', ''))}). "
                        "Research framing only; nothing here recommends buying or selling any security.", st["small"])]

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return buf.getvalue()


def report_filename(run: dict) -> str:
    return f"markut-{run.get('ticker', 'run')}-{(run.get('started_at') or '')[:10] or run.get('id', '')}.pdf"
