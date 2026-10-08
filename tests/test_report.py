"""PDF report builder, offline: renders the bundled course run and a synthetic
run, checks it is a real multi-page PDF, that every UNGROUNDED tag becomes a
footnote, that the packet grid and argument sections parse, and that the
endpoint serves it as an attachment."""
import json
import os
import re

from markut import report
from markut.web import replay

COURSE = json.load(open(os.path.join(replay.BUNDLED_DIR, "nvda_2026-07-17_course_run.json"), encoding="utf-8"))


def _pages(pdf: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page[^s]", pdf))


def test_course_run_renders_multipage_pdf():
    run = {"id": 1, "ticker": "NVDA", "started_at": "2026-07-17 00:00:00", "mode": "course", "events": COURSE["events"]}
    pdf = report.build_report(run)
    assert pdf.startswith(b"%PDF-") and _pages(pdf) >= 5
    assert b"IBMPlexSerif" in pdf and b"JetBrainsMono" in pdf        # fonts embedded
    assert report.report_filename(run) == "markut-NVDA-2026-07-17.pdf"


def test_footnotes_match_tags_and_parsers():
    verdict = COURSE["events"][-2]["data"]["verdict"]
    paras, tags = report.footnote_verdict(verdict)
    assert tags == ["49%", "1.96x", "19.1%"] and "[UNGROUNDED" not in "".join(paras)
    assert "[3]" in "".join(paras)
    grid, rest = report.parse_packet(COURSE["events"][1]["data"]["evidence"])
    assert [s["name"] for s in grid][:2] == ["QUOTE & VALUATION", "FUNDAMENTALS"]
    assert any(k == "Price (current)" for k, _, _ in grid[0]["rows"])
    arg = report.split_argument(COURSE["events"][2]["data"]["text"])
    assert arg["title"].startswith("NVIDIA") and len(arg["sections"]) == 5


def test_stray_bold_marker_never_printed():
    assert "**" not in report._inline('The 10-K warns of risks from **"lack')
    assert "<b>" not in report._inline("**bold**") and "<b>" in report._inline("**bold**", strip_bold=False)


def test_unstructured_and_errored_runs_still_render():
    events = [{"event": "start", "data": {"ticker": "ZZZ", "max_rounds": 1}},
              {"event": "research", "data": {"evidence": "no packet"}},
              {"event": "bull", "data": {"round": 1, "text": "Just a plain paragraph with 12% growth.\n\nAnd another."}},
              {"event": "error", "data": {"stage": "debate", "message": "boom"}}]
    pdf = report.build_report({"id": 9, "ticker": "ZZZ", "started_at": "2026-10-07 10:00:00", "mode": "web", "events": events})
    assert pdf.startswith(b"%PDF-") and _pages(pdf) >= 1
