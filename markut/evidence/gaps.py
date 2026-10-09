"""Data-source health for the evidence packet (audit of run #9, item 5).

Every evidence section fails soft with an inline marker such as
"[dcf section unavailable: Expecting value: line 1 column 1 (char 0)]".
That is honest but ugly: raw parser text reaches the agents and the page.
This module (PURE) rewrites each marker to a clean reason, collects them, and
prepends ONE "[DATA GAPS]" block so a reader sees up front which sources
were missing — including the sources the packet never has (earnings-call
transcripts), listed as known gaps rather than silently absent."""
import re

# raw error text -> what a reader needs to know
_REASONS = [
    (r"\b402\b|subscription", "not covered by the FMP subscription (HTTP 402)"),
    (r"Expecting value|JSONDecodeError|not valid JSON|non-JSON", "upstream returned a non-JSON response"),
    (r"timed? ?out|ReadTimeout|ConnectTimeout", "upstream timed out"),
    (r"\b(401|403)\b|Forbidden|Unauthorized", "upstream refused the request (auth)"),
    (r"\b429\b|rate limit|Too Many", "upstream rate limit"),
    (r"\b5\d\d\b|Server Error|Bad Gateway", "upstream server error"),
    (r"ConnectionError|Name or service not known|nodename|resolve", "network error reaching upstream"),
    (r"KeyError|NoneType|index out of range|missing", "upstream response was missing expected fields"),
]
# marker shapes the evidence builders emit; group 1 = the source name as written
_MARKER_RE = re.compile(
    r"\[(?P<name>[A-Za-z0-9 _/&()'.-]{2,60}?)\s*(?:section\s+)?unavailable:\s*(?P<reason>[^\]]*)\]"
    r"|\[(?P<kind>not extracted|theme unavailable|theme thin|press release unavailable|filings unavailable|news unavailable|no filing documents indexed|section unavailable)[:\s]*(?P<detail>[^\]]*)\]")

# sources the packet can never contain — said once, every run
KNOWN_GAPS = [
    "earnings-call transcript and management multi-year targets (no licensed free source)",
]


def clean_reason(raw: str) -> str:
    text = (raw or "").strip()
    for pat, reason in _REASONS:
        if re.search(pat, text, re.IGNORECASE):
            return reason
    text = re.sub(r"\s+", " ", text)
    return (text[:70] + "…") if len(text) > 70 else (text or "unavailable")


def _name_for(m) -> str:
    if m.group("name"):
        return m.group("name").strip()
    kind, detail = m.group("kind"), (m.group("detail") or "").strip()
    if kind == "not extracted":
        q = re.search(r"'([^']+)'", detail)
        return f"filings: {q.group(1)} block" if q else "filings block"
    if kind in ("theme unavailable", "theme thin"):
        q = re.search(r"for '([^']+)'", detail)
        return f"filings theme: {q.group(1)}" if q else "filings theme"
    return {"press release unavailable": "8-K press release", "filings unavailable": "SEC filings",
            "news unavailable": "news", "no filing documents indexed": "SEC filings",
            "section unavailable": "filing section"}.get(kind, kind)


def summarize_gaps(packet: str, position: str = "end") -> tuple:
    """-> (clean_packet, gaps). gaps = [{"source", "reason"}] in packet order,
    deduped. Raw error text inside markers is replaced by its clean reason;
    the DATA GAPS block is appended (position="end", the packet's last
    section beside the coverage gaps) or prepended (position="start")."""
    gaps, seen = [], set()

    def rewrite(m):
        name = _name_for(m)
        raw = m.group("reason") if m.group("reason") is not None else (m.group("detail") or "")
        kind = m.group("kind") or ""
        if kind == "theme thin":
            reason = "below the similarity floor — omitted rather than quoting noise"
        elif kind == "not extracted":
            reason = "not found in the indexed filings"
        else:
            reason = clean_reason(raw)
        key = (name.lower(), reason)
        if key not in seen:
            seen.add(key)
            gaps.append({"source": name, "reason": reason})
        return f"[{name}: unavailable — {reason}]"

    body = _MARKER_RE.sub(rewrite, packet or "")
    if "[DATA GAPS]" in body:
        return body, gaps  # already summarized
    lines = ["[DATA GAPS]"]
    if gaps:
        lines += [f"- {g['source']}: {g['reason']}" for g in gaps]
    else:
        lines.append("- none — every evidence source responded")
    lines += [f"- known gap (always): {k}" for k in KNOWN_GAPS]
    block = "\n".join(lines)
    if position == "start":
        return block + "\n\n" + body, gaps
    return body.rstrip() + "\n\n" + block, gaps


def count_gaps(packet: str) -> int:
    # how many sources were unavailable, read back from the block
    m = re.search(r"^\[DATA GAPS\]\n((?:- .*\n?)+)", packet or "", re.M)
    if not m:
        return 0
    return sum(1 for ln in m.group(1).splitlines()
               if ln.startswith("- ") and not ln.startswith("- none") and not ln.startswith("- known gap"))
