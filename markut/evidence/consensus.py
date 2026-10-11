"""Analyst consensus — kept OUT of the debate (2026-10-11).

WHY separate: the user wants the debate's lean to come from the evidence and
the arguments alone, with what analysts think shown NEXT TO it as a check
("are we in line, or seeing something they are not?"). If ratings and price
targets sat in the evidence packet, the lean could simply echo them and the
comparison would mean nothing. So the packet carries no ratings or price
targets, and this module reads them separately, after the fact, for display.
Analyst EPS forecasts stay in the packet: they are forecasts of the business
the valuation math runs on, not an opinion about the stock.

Scale: five positions, left (bearish / strong sell) to right (bullish /
strong buy). The debate's lean and the analysts' mean rating (1.0 = strong
buy … 5.0 = strong sell, the provider's convention) are placed on the SAME
positions so the two markers can be read against each other.
"""
LEANS = ["strongly_bearish", "bearish", "neutral", "bullish", "strongly_bullish"]
LEAN_LABELS = {l: l.replace("_", " ") for l in LEANS}
ANALYST_STOPS = ["strong sell", "sell", "hold", "buy", "strong buy"]      # same positions as LEANS
_COUNT_KEYS = [("strongBuy", "strong_buy"), ("buy", "buy"), ("hold", "hold"), ("sell", "sell"), ("strongSell", "strong_sell")]


def lean_position(lean):
    """PURE. 0.0 (strongly bearish) … 1.0 (strongly bullish); None if unknown."""
    return LEANS.index(lean) / 4 if lean in LEANS else None


def analyst_position(mean):
    """PURE. The provider's mean rating (1 strong buy … 5 strong sell) on the
    same 0..1 axis as the lean."""
    try:
        m = float(mean)
    except (TypeError, ValueError):
        return None
    return min(1.0, max(0.0, (5.0 - m) / 4.0))


def analyst_label(mean):
    """PURE. The rating word for a mean, by nearest stop (2.2 -> buy)."""
    pos = analyst_position(mean)
    return None if pos is None else ANALYST_STOPS[min(4, int(round(pos * 4)))]


def analyst_consensus(info: dict, summary_rows, targets) -> dict:
    """PURE. Provider fields -> {mean, label, n, counts, target_mean,
    target_high, target_low, price, source}. counts is None when the
    provider has no rating-spread table. None when there is no rating at
    all — never an invented consensus."""
    info = info or {}
    counts = None
    row = next((r for r in (summary_rows or []) if str(r.get("period", "0m")) == "0m"), None) or (summary_rows or [None])[0]
    if row:
        try:
            counts = {k: int(row.get(src) or 0) for src, k in _COUNT_KEYS}
        except (TypeError, ValueError):
            counts = None
    mean = info.get("recommendationMean")
    if mean is None and counts and sum(counts.values()):
        # weighted mean of the spread on the provider's 1..5 convention
        weights = {"strong_buy": 1, "buy": 2, "hold": 3, "sell": 4, "strong_sell": 5}
        mean = sum(weights[k] * v for k, v in counts.items()) / sum(counts.values())
    if mean is None:
        return None
    try:
        mean = round(float(mean), 2)
    except (TypeError, ValueError):
        return None
    n = info.get("numberOfAnalystOpinions") or (sum(counts.values()) if counts else None)
    t = targets or {}
    return {"mean": mean, "label": analyst_label(mean), "n": int(n) if n else None, "counts": counts,
            "target_mean": t.get("mean") or info.get("targetMeanPrice"), "target_high": t.get("high"), "target_low": t.get("low"),
            "price": info.get("currentPrice") or info.get("regularMarketPrice"),
            "source": "yfinance (recommendation mean, ratings spread, analyst price targets)"}


def fetch_analyst_consensus(ticker: str):
    """The provider read (network, free). Any failure -> None: the page then
    says no consensus was available — the debate itself never depends on it."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        info = t.info or {}
        try:
            summary = t.recommendations_summary
            rows = summary.to_dict("records") if summary is not None and len(summary) else None
        except Exception:
            rows = None
        try:
            targets = t.analyst_price_targets or None
        except Exception:
            targets = None
        return analyst_consensus(info, rows, targets)
    except Exception as e:
        print(f"CONSENSUS: unavailable ({type(e).__name__}: {e})")
        return None


def compare_lean(lean, consensus, withheld: bool = False) -> str:
    """PURE. One plain sentence: where the debate's lean sits against the
    analysts. Half a step either way is 'in line'; two steps or more is
    'much more'. A withheld lean says so (run #28); no lean at all -> ''."""
    if withheld:
        if consensus and analyst_position(consensus.get("mean")) is not None:
            return (f"No lean was issued, so there is nothing to compare; the analyst consensus ({consensus['mean']:g}, "
                    f"{consensus.get('label') or analyst_label(consensus['mean'])}"
                    + (f", {consensus['n']} analysts" if consensus.get("n") else "") + ") is shown for reference only.")
        return "No lean was issued, and no analyst consensus was available."
    p = lean_position(lean)
    if p is None:
        return ""
    if not consensus or analyst_position(consensus.get("mean")) is None:
        return "No analyst consensus was available to compare with the debate's lean."
    a = analyst_position(consensus["mean"])
    who = (f"the analyst consensus ({consensus['mean']:g}, {consensus.get('label') or analyst_label(consensus['mean'])}"
           + (f", {consensus['n']} analysts" if consensus.get("n") else "") + ")")
    diff = p - a
    if abs(diff) <= 0.125:
        return f"The debate's lean is in line with {who}."
    side = "constructive" if diff > 0 else "cautious"
    much = "much " if abs(diff) >= 0.5 else ""
    return f"The debate leans {much}more {side} than {who}."
