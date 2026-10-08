"""Yahoo RSS news pipeline: persistent store, epistemic statuses, packet
formatting, related leads (notebook cell 26, verbatim; cache dir from
markut.config; sentence/budget helpers imported from the RAG module
where the notebook shared them via the global namespace)."""
from markut import config
import json
import os
import re
import time

import requests
from bs4 import BeautifulSoup

from markut.evidence.edgar import EDGAR_CACHE, ticker_to_cik
from markut.evidence.rag import (split_sentences, truncate_to_budget,
    CHARS_PER_TOKEN, get_embedder)

import hashlib
import html as html_lib
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

# ---------------- Yahoo news RSS: bounded, cached, source-tagged ----------------
# WHY Yahoo ONLY (Google News RSS removed): Google's <link> values are
# news.google.com redirect wrappers we refuse to decode (undocumented,
# brittle), and its descriptions echo the
# title, so derive_snippet always returned None. Every Google item therefore
# hydrated as headline-only — exactly the status the evidence packet must
# exclude. The feed was being fetched, scored, and deduped only to be
# discarded: pure cost, zero packet text. Recency ("last 14 days") no longer
# comes from Google's when:14d query param; it moves to a client-side pubDate
# window over an accumulating Yahoo store.
# Yahoo items carry DIRECT publisher links and often a real description
# snippet — depth we can actually use without scraping.
YAHOO_RSS_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline"
NEWS_CACHE_DIR = config.NEWS_CACHE_DIR
os.makedirs(NEWS_CACHE_DIR, exist_ok=True)
NEWS_USER_AGENT = "MarkutResearch/1.0 (educational financial research)"
NEWS_RSS_TTL_SECONDS = 6 * 60 * 60
NEWS_ARTICLE_TTL_SECONDS = 24 * 60 * 60
NEWS_BASELINE_DAYS = 30  # AUDIT FIX (run #2): 14 days missed the week's biggest story; ranking is by materiality, not recency
# WHY 9 (was 5): a headline+status+source record costs ~25 tokens, so breadth
# is cheap. Depth is capped by access walls (most bodies can't be fetched), so
# breadth of INDEPENDENT signals is what the 800-token budget should buy.
# Selection stops early if the running estimate would blow the cap, so 9 is a
# ceiling, not a promise.
NEWS_BASELINE_MAX = 9
NEWS_BASELINE_TOKEN_CAP = 1200
# Snippet mining thresholds — a description only earns packet tokens when it
# says meaningfully MORE than the headline (see derive_snippet).
NEWS_SNIPPET_MIN_EXTRA = 40
NEWS_SNIPPET_MAX_CHARS = 300
# Body excerpts: ~2-3 sentences around the most relevant one (~100 tokens).
# Depth is capped by access walls anyway, so the budget stays small; breadth of
# independent stories is what the packet should spend its tokens on.
NEWS_BODY_MAX_CHARS = 800
# Known paywalls/bot-walls: body fetches against these NEVER return usable
# article text — attempting them only wastes time and pollutes the 24h attempt
# cache with guaranteed failures. Case-insensitive SUBSTRING match against both
# the publisher label and the URL domain, with whitespace stripped from the
# label so "Seeking Alpha" and "seekingalpha.com" both hit "seekingalpha".
BLOCKED_PUBLISHERS = ["seekingalpha", "barchart", "moomoo", "fool.com", "investors.com", "zacks"]
NEWS_TARGET_MAX_CLAIMS = 3
# Related-leads attachment (the demoted successor of targeted verification):
RELATED_LEADS_PER_CLAIM = 3
# WHY 0.30: MiniLM cosine on SHORT text pairs (claim vs headline+snippet) runs
# lower than on filing chunks — related pairs usually land around 0.4-0.6 and
# topical noise sits under ~0.25. Deliberately a notch below the filings RAG's
# 0.35 floor because these texts are shorter and lexically sparser. Below the
# floor we SAY so and show baseline-ranked items instead of faking relevance.
RELATED_LEADS_SIM_FLOOR = 0.30

_FINANCE_TERMS = {
    "acquisition", "analyst", "antitrust", "bankruptcy", "buyback", "capital",
    "cash", "competition", "cybersecurity", "debt", "demand", "earnings",
    "export", "forecast", "fraud", "guidance", "investigation", "lawsuit",
    "margin", "merger", "outlook", "profit", "regulation", "revenue", "risk",
    "sales", "sanctions", "SEC", "tariff"
}
_STOP_WORDS = {
    "about", "after", "against", "because", "before", "being", "between",
    "claim", "could", "does", "from", "have", "into", "made", "major",
    "more", "named", "other", "packet", "provides", "specific", "support",
    "than", "that", "their", "there", "these", "they", "this", "those",
    "unsupported", "which", "with", "would"
}
_LEGAL_SUFFIXES = {"corp", "corporation", "inc", "incorporated", "ltd", "limited", "plc", "company", "co", "holdings"}


def _news_cache_path(prefix: str, key: str) -> str:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return os.path.join(NEWS_CACHE_DIR, f"{prefix}_{digest}.json")


def _load_ttl_json(path: str, ttl_seconds: int):
    try:
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if time.time() - float(data.get("fetched_at", 0)) > ttl_seconds:
            return None
        return data
    except Exception:
        return None


def _save_json(path: str, data: dict):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"NEWS: cache write skipped ({e})")


def company_identity(ticker: str) -> dict:
    """Resolve a ticker to SEC's official company title without another paid API."""
    symbol = ticker.upper().strip()
    ticker_to_cik(symbol)  # also ensures the official mapping cache exists
    path = os.path.join(EDGAR_CACHE, "company_tickers.json")
    with open(path, encoding="utf-8") as f:
        mapping = json.load(f)
    for entry in mapping.values():
        if entry.get("ticker", "").upper() == symbol:
            return {"ticker": symbol, "company": entry.get("title") or symbol}
    return {"ticker": symbol, "company": symbol}


def clean_news_text(value: str) -> str:
    if not value:
        return ""
    text = BeautifulSoup(html_lib.unescape(value), "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", text).strip()


def _rss_datetime_iso(raw: str) -> str:
    # WHY shared: both feeds use RFC-822 pubDate strings ("Wed, 15 Jul 2026 ...").
    # One parser means one set of timezone bugs, not two copies drifting apart.
    try:
        published_dt = parsedate_to_datetime(raw.strip())
        if published_dt.tzinfo is None:
            published_dt = published_dt.replace(tzinfo=timezone.utc)
        return published_dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return ""


def derive_snippet(title: str, summary: str):
    # WHY mine descriptions: the RSS description arrived in the SAME response as
    # the headline — free depth, no extra fetch, no access wall to fight. But
    # feeds sometimes just echo the headline as the description, and an echo
    # is worth zero packet tokens. A description earns snippet status only when:
    #   (a) it is at least NEWS_SNIPPET_MIN_EXTRA chars longer than the title, AND
    #   (b) the title does not simply sit at the start or end of it (a
    #       prefix/suffix echo dressed up with boilerplate).
    title_norm = re.sub(r"\s+", " ", title or "").strip().lower()
    summary_norm = re.sub(r"\s+", " ", summary or "").strip().lower()
    if not summary_norm or len(summary_norm) < len(title_norm) + NEWS_SNIPPET_MIN_EXTRA:
        return None
    if title_norm and (summary_norm.startswith(title_norm) or summary_norm.endswith(title_norm)):
        return None
    # Reuse the notebook-wide truncation convention (complete sentence preferred,
    # word-boundary fallback, visible marker) instead of inventing a second one.
    return truncate_to_budget(summary.strip(), NEWS_SNIPPET_MAX_CHARS)


def _publisher_from_link(link: str) -> str:
    # WHY: Yahoo's feed has no per-item <source> tag, so the link
    # domain is the only publisher signal available. Deriving the label from the
    # URL is honest — we never claim more provenance than the feed actually gives.
    netloc = urlparse(link).netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    if netloc == "finance.yahoo.com":
        return "Yahoo Finance"
    return netloc or "Unknown publisher"


def parse_yahoo_rss(xml_text: str) -> list:
    """Pure parser for Yahoo's per-ticker RSS. Emits the pipeline's canonical
    item shape (title/publisher/link/published/summary/snippet/source_feed)."""
    root = ET.fromstring(xml_text)
    items = []
    for node in root.findall(".//item"):
        link = (node.findtext("link") or "").strip()
        # No " - Publisher" suffix stripping here: Yahoo titles don't carry it.
        title = clean_news_text(node.findtext("title") or "")
        summary = clean_news_text(node.findtext("description") or "")
        items.append({
            "title": title,
            "publisher": _publisher_from_link(link),
            "link": link,
            "published": _rss_datetime_iso(node.findtext("pubDate") or ""),
            "summary": summary,
            "snippet": derive_snippet(title, summary),
            "source_feed": "yahoo_rss",
        })
    return items


def _yahoo_store_path(symbol: str) -> str:
    # Human-readable filename (unlike the hashed fetch-cache names) so the
    # store can be inspected or deleted by hand while debugging/grading.
    return os.path.join(NEWS_CACHE_DIR, f"yahoo_store_{symbol}.json")


def _news_item_key(item: dict) -> str:
    # Canonical identity for cross-fetch dedup: the link normalized to
    # domain+path — Yahoo re-serves the same story with varying tracking query
    # params, which must not create duplicates. Fall back to the normalized
    # title for the rare item without a link.
    parsed = urlparse((item.get("link") or "").strip())
    if parsed.netloc:
        return f"{parsed.netloc}{parsed.path}".lower().rstrip("/")
    return re.sub(r"\s+", " ", (item.get("title") or "").strip().lower())


def _load_yahoo_store(symbol: str) -> list:
    # Fail-soft BY DESIGN: a missing or corrupt store file must never take
    # down the news pillar — warn, start fresh, keep going.
    path = _yahoo_store_path(symbol)
    try:
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            print(f"NEWS STORE: unexpected shape in {path}; starting fresh")
            return []
        return [item for item in data if isinstance(item, dict)]
    except Exception as e:
        print(f"NEWS STORE: could not read {path} ({e}); starting fresh")
        return []


def _update_yahoo_store(symbol: str, fresh_items: list) -> list:
    # APPEND + DEDUP + WINDOW. WHY a persistent store at all: the feed is a
    # rolling latest-N (~20-50 items, no pagination, no date params), so one
    # fetch may span a single busy day. Accumulating fetches across runs is
    # the only way to actually cover the baseline window. The pubDate cut
    # below is the client-side equivalent of the removed Google when:14d
    # query parameter.
    now = time.time()
    merged, dropped_old, dropped_undated = {}, 0, 0
    for item in _load_yahoo_store(symbol) + list(fresh_items):
        ts = _published_timestamp(item)
        if ts == 0:
            # WHY drop undated entries: an item that cannot prove its age can
            # never satisfy a recency window — and it would never EXPIRE, so
            # keeping it would grow the store without bound.
            dropped_undated += 1
            continue
        if now - ts > NEWS_BASELINE_DAYS * 86400:
            dropped_old += 1
            continue
        key = _news_item_key(item)
        if not key:
            continue
        merged[key] = item  # later copy wins: the freshest parse of a story
    items = sorted(merged.values(), key=_published_timestamp, reverse=True)
    try:
        with open(_yahoo_store_path(symbol), "w", encoding="utf-8") as f:
            json.dump(items, f)
    except Exception as e:
        print(f"NEWS STORE: write skipped ({e})")
    note = ""
    if dropped_old:
        note += f" | expired {dropped_old}"
    if dropped_undated:
        note += f" | undated dropped {dropped_undated}"
    print(f"NEWS STORE: {symbol} holds {len(items)} item(s) in the {NEWS_BASELINE_DAYS}d window{note}")
    return items


def fetch_yahoo_rss(ticker: str) -> list:
    """Fetch (6h-throttled) + accumulate. Returns the persistent store's items
    for the last NEWS_BASELINE_DAYS, newest first — NOT just the latest feed
    page. Every selection path (baseline, pool, audit) draws from this."""
    # The 6h TTL raw-XML cache is UNCHANGED and still decides whether we hit
    # the network at all; the persistent store sits BEHIND the throttle, so
    # accumulation never adds requests. Parsing re-runs on every call so
    # parser fixes also apply to already-cached XML.
    symbol = ticker.upper().strip()
    path = _news_cache_path("rss_yahoo", symbol)
    cached = _load_ttl_json(path, NEWS_RSS_TTL_SECONDS)
    if cached is not None:
        latest = parse_yahoo_rss(cached.get("xml", ""))
    else:
        try:
            response = requests.get(
                YAHOO_RSS_URL,
                params={"s": symbol, "region": "US", "lang": "en-US"},
                headers={"User-Agent": NEWS_USER_AGENT},
                timeout=15,
            )
            response.raise_for_status()
            _save_json(path, {"fetched_at": time.time(), "xml": response.text})
            latest = parse_yahoo_rss(response.text)
        except Exception as e:
            # WHY serve the store on a dead feed: the whole point of
            # accumulating is resilience — yesterday's stored stories beat an
            # error marker. The [news unavailable] contract still holds when
            # BOTH the fetch fails AND the store is empty (re-raise below).
            stored = _update_yahoo_store(symbol, [])
            if stored:
                print(f"NEWS: Yahoo fetch failed ({e}); serving {len(stored)} stored item(s)")
                return stored
            raise
    return _update_yahoo_store(symbol, latest)


def _identity_terms(company: str) -> list:
    terms = re.findall(r"[A-Za-z0-9]+", company.lower())
    return [term for term in terms if len(term) > 2 and term not in _LEGAL_SUFFIXES]


def _item_tokens(item: dict) -> set:
    return set(re.findall(r"[a-z0-9]+", f"{item.get('title', '')} {item.get('summary', '')}".lower()))


def _claim_terms(claim: str) -> list:
    words = re.findall(r"[A-Za-z0-9.$%+-]+", claim)
    kept = []
    for word in words:
        plain = word.lower().strip(".$%+-")
        if (any(ch.isdigit() for ch in word) or len(plain) >= 5) and plain not in _STOP_WORDS:
            if plain not in kept:
                kept.append(plain)
    return kept[:10]


def _published_timestamp(item: dict) -> float:
    try:
        return datetime.fromisoformat(item.get("published", "")).timestamp()
    except Exception:
        return 0.0


# AUDIT FIX (run #2): MATERIALITY. A per-ticker feed is dominated by listicles
# ("What will $5,000 become..."); the Oct 1 Reuters report on a $42B loan was in
# the window and lost on recency. Stories about financing, guidance, deals,
# litigation and ratings moves carry the debate; each distinct term below adds
# weight (capped), and recency decays over the whole window instead of a week.
MATERIAL_TERMS = {
    "loan": 1.5, "financing": 1.5, "debt": 1.0, "bond": 1.0, "credit": 0.75, "guarantee": 1.5,
    "guidance": 1.5, "outlook": 1.0, "forecast": 1.0, "raises": 1.0, "cuts": 1.0, "misses": 1.0, "beats": 1.0,
    "acquisition": 1.5, "acquire": 1.5, "acquires": 1.5, "merger": 1.5, "deal": 1.0, "contract": 1.0, "agreement": 1.0,
    "downgrade": 1.5, "downgrades": 1.5, "upgrade": 1.0, "upgrades": 1.0, "lawsuit": 1.5, "sues": 1.5,
    "investigation": 1.5, "probe": 1.0, "recall": 1.0, "layoffs": 1.0, "ceo": 1.0, "resigns": 1.0,
    "buyback": 1.0, "dividend": 0.5, "earnings": 1.0, "revenue": 0.75, "billion": 0.75, "reuters": 0.5, "bloomberg": 0.5,
}
MATERIALITY_CAP = 4.0


def materiality_score(item: dict) -> float:
    # PURE. Sum of MATERIAL_TERMS weights for distinct terms in title+summary
    # (title hits count double), capped so one dense headline cannot dominate.
    tokens = _item_tokens(item)
    title_tokens = set(re.findall(r"[a-z0-9]+", item.get("title", "").lower()))
    score = 0.0
    for term, w in MATERIAL_TERMS.items():
        if term in tokens:
            score += w * (2 if term in title_tokens else 1)
    return min(MATERIALITY_CAP, score)


def score_news_item(item: dict, identity: dict, context_query: str = "", now: float = None) -> float:
    # WHY the now parameter: recency reads the clock, so two calls microseconds
    # apart score the SAME item differently at the ~1e-10 level. True ties
    # (identical story + pubDate from both feeds) would then never be exactly
    # equal and the snippet tie-break in select_news_items could never engage.
    # Callers ranking a pool pass ONE frozen `now` so every candidate is scored
    # against the same instant.
    if now is None:
        now = time.time()
    tokens = _item_tokens(item)
    title_tokens = set(re.findall(r"[a-z0-9]+", item.get("title", "").lower()))
    company_terms = _identity_terms(identity["company"])
    ticker = identity["ticker"].lower()
    company_hits = sum(term in tokens for term in company_terms)
    ticker_hit = ticker in tokens and len(ticker) >= 3
    # WHY identity over title+summary COMBINED, and ticker OR company name:
    # (a) title-only matching would over-filter the exact items that carry
    #     text depth — plenty of stories name the company only in the blurb;
    # (b) a per-ticker feed's headlines often say "NVDA", never "NVIDIA", so
    #     with Yahoo as the ONLY source the ticker must count as identity.
    #     (Ticker counts only at 3+ letters: a 1-2 letter ticker like KO
    #     collides with ordinary words, and its stories name the company.)
    # An item with NO identity token anywhere is about something else: reject.
    if company_terms and company_hits == 0 and not ticker_hit:
        return -1e6
    score = company_hits * 3 + sum(term in title_tokens for term in company_terms) * 2
    score += 1.5 if ticker in tokens else 0
    score += sum(term.lower() in tokens for term in _FINANCE_TERMS) * 0.4
    query_terms = set(_claim_terms(context_query))
    score += len(query_terms & tokens) * 1.25
    score += materiality_score(item)
    age_days = max(0, (now - _published_timestamp(item)) / 86400) if _published_timestamp(item) else NEWS_BASELINE_DAYS
    # gentle recency: 1.5 points at zero age, zero at the window edge — a
    # material story from day 20 can outrank a fresh listicle
    score += max(0, 1.5 * (1 - age_days / NEWS_BASELINE_DAYS))
    return score


def _near_duplicate(a: dict, b: dict) -> bool:
    ta = set(re.findall(r"[a-z0-9]+", a.get("title", "").lower()))
    tb = set(re.findall(r"[a-z0-9]+", b.get("title", "").lower()))
    if not ta or not tb:
        return False
    return len(ta & tb) / min(len(ta), len(tb)) >= 0.75


def _estimated_item_chars(item: dict) -> int:
    # Estimate what this item will COST in the packet BEFORE hydration, using
    # only facts known at selection time:
    #   headline - rendered capped at 220 chars
    #   depth    - a direct non-blocked link may earn body text (bounded by
    #              NEWS_BODY_MAX_CHARS); anything else can at most render its
    #              snippet (~300-capped; None costs nothing)
    #   overhead - Status + source tag (feed/publisher, date, URL) + labels
    link = item.get("link", "")
    may_fetch_body = not _is_blocked_publisher(item.get("publisher", ""), link)
    depth = NEWS_BODY_MAX_CHARS if may_fetch_body else len(item.get("snippet") or "")
    return (min(len(item.get("title", "")), 220) + depth
            + len(item.get("publisher", "")) + len(link) + 90)


def select_news_items(items: list, identity: dict, limit: int, context_query: str = "",
                      token_cap: int = None) -> list:
    # (The Google-era cross-feed snippet tie-break is gone: with one source
    # there are no competing copies of a story from different feeds to
    # arbitrate. Dedup below is within-Yahoo only.)
    now = time.time()  # frozen once — see score_news_item's now parameter
    ranked = sorted(items,
                    key=lambda item: score_news_item(item, identity, context_query, now),
                    reverse=True)
    # WHY stop on an ESTIMATE here when format_news_items already hard-enforces
    # the real cap: selecting an item we cannot afford means hydrating it
    # (network fetches) only for formatting to pop it again. Stopping at
    # selection time spends the budget once, deliberately, on the top-ranked
    # run of affordable items. token_cap=None (targeted path) skips the check.
    char_budget = token_cap * CHARS_PER_TOKEN if token_cap else None
    estimated_chars = 0
    selected, publisher_counts = [], {}
    for item in ranked:
        if score_news_item(item, identity, context_query, now) < 0:
            continue
        publisher = item.get("publisher", "Unknown publisher").lower()
        if publisher_counts.get(publisher, 0) >= 2:
            continue
        if any(_near_duplicate(item, prior) for prior in selected):
            continue
        if char_budget is not None and estimated_chars + _estimated_item_chars(item) > char_budget:
            break  # cap spent — more breadth would cost items the packet can't hold
        estimated_chars += _estimated_item_chars(item)
        selected.append(dict(item))
        publisher_counts[publisher] = publisher_counts.get(publisher, 0) + 1
        if len(selected) == limit:
            break
    return selected


def _is_blocked_publisher(publisher: str, url: str) -> bool:
    # Normalize the label by lowercasing and dropping spaces so "Seeking Alpha"
    # (publisher) and "seekingalpha.com" (domain) both hit the same list entry.
    label = re.sub(r"\s+", "", (publisher or "").lower())
    domain = (urlparse(url or "").netloc or "").lower()
    return any(entry in label or entry in domain for entry in BLOCKED_PUBLISHERS)


def _resolve_news_url(url: str) -> str:
    if not url:
        return ""
    try:
        with requests.get(url, headers={"User-Agent": NEWS_USER_AGENT}, timeout=8,
                          allow_redirects=True, stream=True) as response:
            return response.url or url
    except Exception:
        return url


def _robots_allows(url: str) -> bool:
    try:
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        response = requests.get(robots_url, headers={"User-Agent": NEWS_USER_AGENT}, timeout=5)
        if response.status_code == 404:
            return True
        if response.status_code >= 400:
            return False
        parser = RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(response.text.splitlines())
        return parser.can_fetch(NEWS_USER_AGENT, url)
    except Exception:
        return False  # fail closed: headline/snippet remains available as a lead


def _extract_article_text(html_text: str) -> str:
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "form", "aside", "noscript"]):
        tag.decompose()
    container = soup.find("article") or soup
    paragraphs, seen = [], set()
    for p in container.find_all("p"):
        text = re.sub(r"\s+", " ", p.get_text(" ")).strip()
        if len(text) < 40 or text in seen:
            continue
        seen.add(text)
        paragraphs.append(text)
        if sum(len(x) for x in paragraphs) >= 7000:
            break
    return "\n\n".join(paragraphs)


def fetch_accessible_article(url: str) -> dict:
    # WHY only direct URLs land here: hydrate_news_item's attempt policy skips
    # blocked publishers BEFORE calling this, so every
    # cache entry below records a GENUINE direct-URL attempt. A cached empty
    # text is a real failed extraction (24h TTL) — never a wrapper artifact or
    # a publisher we already knew would refuse us.
    path = _news_cache_path("article", url)
    cached = _load_ttl_json(path, NEWS_ARTICLE_TTL_SECONDS)
    if cached is not None:
        return {"url": cached.get("url", url), "text": cached.get("text", "")}
    resolved = _resolve_news_url(url)
    parsed = urlparse(resolved)
    text = ""
    if parsed.scheme in ("http", "https") and _robots_allows(resolved):
        try:
            response = requests.get(resolved, headers={"User-Agent": NEWS_USER_AGENT}, timeout=10)
            response.raise_for_status()
            if "text/html" in response.headers.get("Content-Type", ""):
                text = _extract_article_text(response.text)
        except Exception:
            text = ""
    result = {"fetched_at": time.time(), "url": resolved or url, "text": text[:7000]}
    _save_json(path, result)
    return {"url": result["url"], "text": result["text"]}


def select_article_excerpt(article_text: str, identity: dict, context_query: str = "") -> str:
    # Pick the most company/claim-relevant sentence, then grow a window around
    # it until we fill NEWS_BODY_MAX_CHARS (cap is a fill target, not just a ceiling).
    sentences = split_sentences(re.sub(r"\s+", " ", article_text))
    if not sentences:
        return ""
    desired = set(_identity_terms(identity["company"]) + _claim_terms(context_query))
    scored = []
    for index, sentence in enumerate(sentences):
        tokens = set(re.findall(r"[a-z0-9]+", sentence.lower()))
        score = len(tokens & desired) * 2 + len(tokens & {t.lower() for t in _FINANCE_TERMS}) * 0.5
        scored.append((score, index))
    _, best = max(scored)
    start, end = best, best + 1
    while True:
        excerpt = " ".join(sentences[start:end])
        if len(excerpt) >= NEWS_BODY_MAX_CHARS:
            break
        can_left, can_right = start > 0, end < len(sentences)
        if not can_left and not can_right:
            break
        # Prefer the side whose next sentence scores higher (more on-topic).
        left_score = scored[start - 1][0] if can_left else -1
        right_score = scored[end][0] if can_right else -1
        if right_score >= left_score and can_right:
            end += 1
        else:
            start -= 1
    return truncate_to_budget(" ".join(sentences[start:end]), NEWS_BODY_MAX_CHARS)


def hydrate_news_item(item: dict, identity: dict, context_query: str = "") -> dict:
    hydrated = dict(item)
    link = item.get("link", "")
    # ATTEMPT POLICY: body-fetch ONLY publisher URLs outside the blocked list
    # (BLOCKED_PUBLISHERS — known walls where attempts always fail). Skips are
    # recorded as skips — only real attempts can ever enter the 24h attempt
    # cache as failures.
    if _is_blocked_publisher(item.get("publisher", ""), link):
        article, fetch_state = {"url": link, "text": ""}, "skipped (blocked publisher)"
    else:
        article = fetch_accessible_article(link)
        fetch_state = "attempted"
    excerpt = select_article_excerpt(article.get("text", ""), identity, context_query)
    if fetch_state == "attempted":
        fetch_state = "fetched body text" if excerpt else "failed (no extractable text)"
    # Epistemic ladder, strongest first: body (real page text, still an
    # untrusted quote) > lead (feed-authored snippet — derive_snippet already
    # applied the only-if-it-beats-the-headline rule) > headline-only.
    if excerpt:
        status, context = "body", excerpt
    elif item.get("snippet"):
        status, context = "lead", item["snippet"]
    else:
        status, context = "headline-only", ""
    hydrated.update({"url": article.get("url") or link, "context": context,
                     "status": status, "fetch_state": fetch_state})
    return hydrated


def hydrate_news_items(items: list, identity: dict, context_query: str = "") -> list:
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(5, len(items))) as pool:
        futures = [pool.submit(hydrate_news_item, item, identity, context_query) for item in items]
        return [future.result() for future in futures]


# Human-readable names for source_feed values ("yahoo_api" is reserved for a
# possible yfinance Ticker.news source). Unknown values render as plain "news".
_FEED_LABELS = {"yahoo_rss": "Yahoo RSS", "yahoo_api": "Yahoo API"}


def format_news_items(items: list, heading: str, coverage: str, token_cap: int) -> str:
    char_cap = token_cap * CHARS_PER_TOKEN
    working = list(items)
    while working:
        fixed = len(heading) + len(coverage) + sum(
            len(item.get("title", "")) + len(item.get("publisher", ""))
            + len(item.get("published", "")) + len(item.get("url", ""))
            + len(item.get("status") or item.get("access", "")) + 90
            for item in working
        )
        context_budget = max(100, (char_cap - fixed) // len(working))
        lines = [heading, coverage]
        for item in working:
            published = item.get("published", "")[:10] or "date unavailable"
            lines.append(f"- Headline: {truncate_to_budget(item.get('title', ''), 220)}")
            context = item.get("context", "")
            # One "lead:" line holds whatever depth the item earned (body text or
            # snippet); the Status line names its epistemic weight:
            #   body          = fetched article text (an untrusted quote, but real page text)
            #   lead          = feed-authored snippet — suggests, cannot establish
            #   headline-only = nothing beyond the title (no lead line printed)
            # Falling back to the legacy "access" field keeps items built before
            # the status vocabulary (offline fixtures) rendering unchanged.
            status = item.get("status") or item.get("access", "headline-only")
            if context:
                lines.append(f"  lead: {truncate_to_budget(context, context_budget)}")
            lines.append(f"  Status: {status}")
            # The tag names the FEED that surfaced the item (provenance).
            feed_label = _FEED_LABELS.get(item.get("source_feed"), "news")
            lines.append(f"  [source: {feed_label}/{item.get('publisher', 'Unknown publisher')}, published {published}, {item.get('url', '')}]")
        text = "\n".join(lines)
        if len(text) <= char_cap:
            return text
        working.pop()  # preserve complete source records; reduce count before eating tags
    return f"{heading}\n[no news items fit within the {token_cap}-token cap]"


def get_news_evidence(ticker: str) -> str:
    try:
        identity = company_identity(ticker)
        # SINGLE source: Yahoo's per-ticker feed (see the Google-removal WHY at
        # the top of this cell). Fail-soft contract unchanged: a dead feed
        # returns the marker line and never crashes the evidence pillar.
        try:
            candidates = fetch_yahoo_rss(identity["ticker"])
        except Exception as e:
            print(f"NEWS: Yahoo feed failed ({e})")
            return f"\n\n[NEWS]\n[news unavailable: yahoo_rss: {e}]"
        # SELECTION SLACK (2x): a body fetch can fail and demote an item to
        # headline-only, which the packet below EXCLUDES — if we selected only
        # the final budget, every such failure would waste a slot that a
        # blurb-bearing item further down the ranking could have filled. So
        # carry ~2x candidates through hydration (both the item ceiling and
        # the selection-time token estimate are doubled), filter by status,
        # then trim back. The REAL 800-token cap is still hard-enforced by
        # format_news_items after the trim, exactly as before.
        selected = select_news_items(candidates, identity, NEWS_BASELINE_MAX * 2,
                                     token_cap=NEWS_BASELINE_TOKEN_CAP * 2)
        hydrated = hydrate_news_items(selected, identity)
        if not hydrated:
            return "\n\n[NEWS]\n[no company-relevant stories found in the baseline window]"
        # EPISTEMIC FLOOR for the evidence packet: a headline-only item carries
        # no text a debater may cite — a bare title on the evidence table only
        # invites treating a headline as a fact. Keep lead (feed blurb) and
        # body (fetched excerpt); drop the rest, and SAY how many were dropped
        # so a thin packet is explainable from the logs.
        usable = [item for item in hydrated if item.get("status") != "headline-only"]
        dropped = len(hydrated) - len(usable)
        if dropped:
            print(f"NEWS: dropped {dropped} headline-only item(s) from the baseline packet")
        if not usable:
            return "\n\n[NEWS]\n[no news items with usable blurb or body in the baseline window]"
        usable = usable[:NEWS_BASELINE_MAX]  # trim the slack back to the real budget
        return "\n\n" + format_news_items(
            usable, "[NEWS]",
            f"Coverage: last {NEWS_BASELINE_DAYS} days | up to {NEWS_BASELINE_MAX} selected stories | "
            "status: body=fetched article text (untrusted quote), lead=feed snippet. "
            "Leads suggest; they cannot establish facts.",
            NEWS_BASELINE_TOKEN_CAP,
        )
    except Exception as e:
        return f"\n\n[NEWS]\n[news unavailable: {e}]"


def get_news_candidate_pool(ticker: str) -> list:
    # READ-ONLY accessor for downstream consumers (the related-leads
    # attachment). Returns the baseline's merged + scored + deduped candidate
    # list BEFORE the final top-N/token-cap selection cut. Reuses the same
    # 6h-cached feed fetches as get_news_evidence — a warm cache means ZERO
    # network — and changes no baseline behavior: nothing here mutates state.
    identity = company_identity(ticker)
    try:
        candidates = fetch_yahoo_rss(identity["ticker"])
    except Exception as e:
        print(f"NEWS POOL: Yahoo feed failed ({e})")
        return []
    if not candidates:
        return []
    # limit=len(candidates) disables the top-N cut, and passing no token_cap
    # disables the budget stop — what remains is exactly the baseline's own
    # scored, deduped ranking of the whole pool.
    return select_news_items(candidates, identity, len(candidates))


def audit_news_selection(ticker: str) -> str:
    # WHY this helper exists: the final [NEWS] packet hides ranking, statuses,
    # and everything that was dropped. This MIRRORS get_news_evidence's exact
    # baseline path (2x slack -> hydrate -> drop headline-only -> trim) while
    # printing what the packet hides — per selected item: rank score, source,
    # status, snippet length, fetch outcome — plus a dropped headline-only
    # count and the persistent store's size and pubDate span, so you can watch
    # coverage of the 14d window ACCUMULATE across runs. Free: network only,
    # no Claude. If get_news_evidence's flow changes, change this mirror too.
    identity = company_identity(ticker)
    symbol = identity["ticker"]
    candidates, feed_errors = [], []
    try:
        candidates = fetch_yahoo_rss(symbol)
    except Exception as e:
        feed_errors.append(f"yahoo_rss: {e}")
        print(f"NEWS AUDIT: Yahoo feed failed ({e})")

    print("\n" + "=" * 70)
    print(f"NEWS AUDIT: {symbol} ({identity['company']})")
    print(f"candidates: {len(candidates)}  |  feed errors: {feed_errors or 'none'}")
    # STORE COVERAGE: the feed is a rolling latest-N, so the store's pubDate
    # span shows how much of the 14d window accumulated runs actually cover.
    stored = _load_yahoo_store(symbol)
    stored_ts = [ts for ts in (_published_timestamp(item) for item in stored) if ts]
    if stored_ts:
        dates = sorted((item.get("published") or "")[:10] for item in stored if item.get("published"))
        span_days = (max(stored_ts) - min(stored_ts)) / 86400
        print(f"store: {len(stored)} item(s) | pubDate span {dates[0]} .. {dates[-1]} "
              f"(~{span_days:.1f} of {NEWS_BASELINE_DAYS} days covered)")
    else:
        print("store: empty — coverage accumulates as runs repeat inside the 14d window")
    print(f"cap: {NEWS_BASELINE_MAX} stories / {NEWS_BASELINE_TOKEN_CAP} tokens")
    print("=" * 70)

    if not candidates:
        msg = f"[NEWS]\n[news unavailable: {'; '.join(feed_errors) or 'empty pool'}]"
        print(msg)
        return msg

    now = time.time()
    selected = select_news_items(candidates, identity, NEWS_BASELINE_MAX * 2,
                                 token_cap=NEWS_BASELINE_TOKEN_CAP * 2)
    hydrated = hydrate_news_items(selected, identity)
    usable = [item for item in hydrated if item.get("status") != "headline-only"]
    print(f"dropped headline-only: {len(hydrated) - len(usable)} (of {len(hydrated)} hydrated)")
    usable = usable[:NEWS_BASELINE_MAX]
    if not usable:
        msg = "[NEWS]\n[no news items with usable blurb or body in the baseline window]"
        print(msg)
        return msg

    for rank, item in enumerate(usable, start=1):
        score = score_news_item(item, identity, now=now)
        published = (item.get("published") or "")[:10] or "date unavailable"
        lead_line = (item.get("context") or item.get("snippet") or "").split("\n", 1)[0]
        print(f"\n  #{rank}  score={score:.2f}  source={item.get('source_feed', '?')}  "
              f"status={item.get('status', 'headline-only')}  "
              f"snippet_len={len(item.get('snippet') or '')}")
        print(f"  publisher: {item.get('publisher', '?')}  |  published: {published}")
        print(f"  fetch: {item.get('fetch_state', '?')}")
        print(f"  title: {item.get('title', '')}")
        print(f"  link:  {item.get('url') or item.get('link', '')}")
        if lead_line:
            print(f"  lead:  {truncate_to_budget(lead_line, 180)}")

    packet = format_news_items(
        usable, "[NEWS]",
        f"Coverage: last {NEWS_BASELINE_DAYS} days | up to {NEWS_BASELINE_MAX} selected stories | "
        "status: body=fetched article text (untrusted quote), lead=feed snippet. "
        "Leads suggest; they cannot establish facts.",
        NEWS_BASELINE_TOKEN_CAP,
    )
    print(f"\n  PACKET: {len(usable)} item(s) | ~{len(packet) // CHARS_PER_TOKEN} tokens "
          f"(cap {NEWS_BASELINE_TOKEN_CAP}) | chars={len(packet)}")
    print("  (body attempts only on non-blocked URLs; Seeking Alpha / Barchart / "
          "Moomoo must show skipped, never attempted)")
    return packet


# ---- RELATED LEADS (demoted successor of targeted news verification) ----
# WHY demoted: news cannot VERIFY claims in this system. The epistemic rule
# says only status "body" or filings-backed evidence may influence a verdict,
# and bodies clear the redirect wrappers and paywalls maybe 10-25% of the
# time — a verifier that fires that rarely makes the rule INCONSISTENT, which
# is worse than no verifier. The old per-claim Google queries were also
# overfitted: judge-written claims arrive full of pipeline vocabulary that
# appears in no news story (last run: 0 items for every claim). So claim
# RESOLUTION happens against the evidence packet via judge revision, and the
# news layer contributes semantically-ranked RELATED LEADS from the pool we
# already fetched — display context only, never proof.

CLAIM_META_WORDS = {
    # Pipeline vocabulary that shows up in judge-written claims but in no
    # story. Left in, it drags the claim embedding toward jargon and away
    # from the claim's actual subject (diagnosed cause #1 of the 0-item runs).
    "lead-only", "evidence", "unsupported", "extrapolation", "verdict",
    "packet", "status", "claim", "judge", "bull", "bear",
}


def clean_claim_for_embedding(claim: str) -> str:
    kept = []
    for token in str(claim).split():
        if token.strip(".,;:()[]{}\"'").lower() in CLAIM_META_WORDS:
            continue
        kept.append(token)
    return " ".join(kept)


def _cosine(a, b) -> float:
    # Plain cosine over two vectors. The vectors come from the SAME lazily
    # loaded MiniLM instance the filings RAG uses (get_embedder) — one model
    # shared by two subsystems, zero extra downloads or instantiations.
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def get_related_leads(unsupported_claims: list, ticker: str) -> str:
    # Per claim: embed the cleaned claim, embed every pool candidate's
    # headline+snippet, rank by cosine, attach the top RELATED_LEADS_PER_CLAIM.
    # WHY ranking instead of search: zero new network calls (the pool is the
    # baseline's cached fetch), it cannot return empty while the pool is
    # non-empty, and there is no per-claim query engineering to maintain —
    # the three failure modes of the search it replaces.
    header = "[RELATED LEADS — context only, NOT verification]"
    claims = [str(c).strip() for c in (unsupported_claims or []) if str(c).strip()]
    claims = claims[:NEWS_TARGET_MAX_CLAIMS]
    if not claims:
        return header + "\n[no unsupported claims to contextualize]"
    try:
        pool = get_news_candidate_pool(ticker)
    except Exception as e:
        return header + f"\n[news pool unavailable: {e}]"
    if not pool:
        return header + "\n[news pool empty — no leads available]"

    # ONE encode call for claims + candidates together: a few dozen short
    # texts through the already-loaded local model. No API tokens, no network.
    cleaned = [clean_claim_for_embedding(c) or c for c in claims]
    cand_texts = [f"{item.get('title', '')} {item.get('snippet') or ''}".strip()
                  for item in pool]
    vectors = get_embedder().encode(cleaned + cand_texts, show_progress_bar=False)
    claim_vecs, cand_vecs = vectors[:len(cleaned)], vectors[len(cleaned):]

    lines = [header]
    for idx, claim in enumerate(claims):
        ranked = sorted(
            ((_cosine(claim_vecs[idx], vec), item) for vec, item in zip(cand_vecs, pool)),
            key=lambda pair: pair[0], reverse=True)
        lines.append("")
        lines.append(f"Claim {idx + 1}: {truncate_to_budget(claim, 400)}")
        best = ranked[0][0]
        if best < RELATED_LEADS_SIM_FLOOR:
            # Honesty over decoration: below the floor we SAY the pool holds
            # nothing close and fall back to the baseline's own ranking, so
            # the block is informative and NEVER an empty string.
            lines.append(f"  [no closely related leads in current pool "
                         f"(best sim {best:.2f}) — top baseline items instead]")
            chosen = [(None, item) for item in pool[:RELATED_LEADS_PER_CLAIM]]
        else:
            chosen = ranked[:RELATED_LEADS_PER_CLAIM]
        for sim, item in chosen:
            # Pool items are UNhydrated — a body fetch would be new network,
            # which this subsystem is forbidden. Snippet-bearing items display
            # as "lead", the rest "headline-only"; nothing here can say "body".
            status = "lead" if item.get("snippet") else "headline-only"
            published = (item.get("published") or "")[:10] or "date unavailable"
            snippet_line = (item.get("snippet") or "").split("\n", 1)[0] or "(no snippet)"
            sim_label = f"sim {sim:.2f}" if sim is not None else "baseline rank"
            lines.append("  - " + " | ".join([
                status,
                item.get("source_feed", "?"),
                item.get("publisher", "Unknown publisher"),
                published,
                truncate_to_budget(item.get("title", ""), 140),
                truncate_to_budget(snippet_line, 160),
                sim_label,
            ]))
    return "\n".join(lines)


def get_targeted_news_evidence(ticker: str, unsupported_claims: list) -> str:
    # DEPRECATED alias — kept only so existing call sites keep working, with
    # the OLD argument order. Targeted news was demoted from claim VERIFIER to
    # related-leads ATTACHMENT; the real implementation is get_related_leads.
    return get_related_leads(unsupported_claims, ticker)


def parse_news_review_json(text: str) -> dict:
    cleaned = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    data = json.loads(cleaned)
    for key in ("claim_reviews", "revised_verdict"):
        if key not in data:
            raise ValueError(f"news review JSON missing required key: {key!r}")
    if not isinstance(data["claim_reviews"], list):
        raise ValueError("news review claim_reviews must be a list")
    allowed = {"supported", "contradicted", "unresolved"}
    for review in data["claim_reviews"]:
        if not isinstance(review, dict):
            raise ValueError("each claim review must be an object")
        status = str(review.get("status", "unresolved")).lower()
        review["status"] = status if status in allowed else "unresolved"
        if not isinstance(review.get("sources"), list):
            review["sources"] = []
        review.setdefault("evidence_summary", "")
    data["revised_verdict"] = str(data["revised_verdict"])
    return data
