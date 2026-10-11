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


RATING_KINDS = {"up": "upgrade", "down": "downgrade", "init": "initiated"}


def _money(v, ccy: str = "USD") -> str:
    v = float(v)
    body = f"{v:,.0f}" if abs(v - round(v)) < 0.005 else f"{v:,.2f}"
    return f"${body}" if (ccy or "USD").upper() == "USD" else f"{ccy.upper()} {body}"


def analyst_actions(rows, today: str = None, days: int = 90, cap: int = 8, ccy: str = "USD") -> dict:
    """PURE. The key recent analyst moves (2026-10-11): upgrades, downgrades,
    initiations and price-target changes in the last `days`, newest first —
    every rating change kept, then the newest target changes up to `cap`; a
    reiteration at an unchanged target is not a move. -> {"items": [{date,
    firm, kind, text}], "summary": counts over the window, "latest": the most
    recent action of any age (named when the window is empty)}."""
    from datetime import date, timedelta
    rows = [r for r in (rows or []) if r.get("date") and r.get("firm")]
    if not rows:
        return {"items": [], "summary": None, "latest": None}
    cutoff = (date.fromisoformat((today or date.today().isoformat())[:10]) - timedelta(days=days)).isoformat()
    num = lambda v: float(v) if v not in (None, "") and v == v and float(v) > 0 else None
    moves = []
    for r in sorted(rows, key=lambda r: str(r["date"]), reverse=True):
        now, prior = num(r.get("pt_now")), num(r.get("pt_prior"))
        kind = RATING_KINDS.get(str(r.get("action", "")).lower())
        if not kind and now and prior and abs(now - prior) > 0.005:
            kind = "target_cut" if now < prior else "target_raise"
        if not kind:
            continue
        to, frm, firm = (r.get("to_grade") or "").strip(), (r.get("from_grade") or "").strip(), r["firm"].strip()
        target = (f"price target {_money(now, ccy)} from {_money(prior, ccy)}" if now and prior and abs(now - prior) > 0.005
                  else f"price target {_money(now, ccy)}" if now else "")
        if kind == "upgrade" or kind == "downgrade":
            text = f"{firm} {kind}d to {to}" + (f" from {frm}" if frm and frm != to else "") + (f"; {target}" if target else "")
        elif kind == "initiated":
            text = f"{firm} initiated at {to}" + (f"; {target}" if target else "")
        else:
            verb = "cut" if kind == "target_cut" else "raised"
            text = f"{firm} {verb} its price target to {_money(now, ccy)} from {_money(prior, ccy)}" + (f" ({to})" if to else "")
        moves.append({"date": str(r["date"])[:10], "firm": firm, "kind": kind, "text": text,
                      "pt_change": bool(now and prior and abs(now - prior) > 0.005), "pt_cut": bool(now and prior and now < prior)})
    recent = [m for m in moves if m["date"] >= cutoff]
    ratings = [m for m in recent if m["kind"] in ("upgrade", "downgrade", "initiated")][:cap]
    targets = [m for m in recent if m not in ratings][:max(0, cap - len(ratings))]
    items = sorted(ratings + targets, key=lambda m: m["date"], reverse=True)
    summary = {"days": days, "upgrades": sum(m["kind"] == "upgrade" for m in recent),
               "downgrades": sum(m["kind"] == "downgrade" for m in recent),
               "initiations": sum(m["kind"] == "initiated" for m in recent),
               "target_cuts": sum(m["pt_cut"] for m in recent),
               "target_raises": sum(m["pt_change"] and not m["pt_cut"] for m in recent)} if recent else None
    return {"items": [{k: m[k] for k in ("date", "firm", "kind", "text")} for m in items], "summary": summary,
            "latest": {k: moves[0][k] for k in ("date", "firm", "kind", "text")} if moves else None}


def target_upside(c) -> float:
    """PURE. % from today's price to the mean analyst target; None without both."""
    try:
        return (float(c["target_mean"]) / float(c["price"]) - 1) * 100
    except (TypeError, ValueError, KeyError, ZeroDivisionError):
        return None


def actions_summary(summary) -> str:
    """'2 downgrades · 10 price-target cuts in the last 90 days' ('' when none)."""
    if not summary:
        return ""
    parts = [(summary["upgrades"], "upgrade"), (summary["downgrades"], "downgrade"), (summary["initiations"], "initiation"),
             (summary["target_raises"], "price-target raise"), (summary["target_cuts"], "price-target cut")]
    shown = [f"{n} {w}{'s' if n != 1 else ''}" for n, w in parts if n]
    return (" · ".join(shown) + f" in the last {summary['days']} days") if shown else ""


def analyst_consensus(info: dict, summary_rows, targets, actions: dict = None) -> dict:
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
            "target_median": t.get("median") or info.get("targetMedianPrice"),
            "price": info.get("currentPrice") or info.get("regularMarketPrice") or t.get("current"),
            "currency": (info.get("currency") or "USD").upper(),
            "actions": actions or {"items": [], "summary": None, "latest": None},
            "source": "yfinance (recommendation mean, ratings spread, analyst price targets, upgrades and downgrades)"}


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
        try:
            ud = t.upgrades_downgrades
            moves = [{"date": str(idx)[:10], "firm": r.get("Firm"), "to_grade": r.get("ToGrade"), "from_grade": r.get("FromGrade"),
                      "action": r.get("Action"), "pt_action": r.get("priceTargetAction"), "pt_now": r.get("currentPriceTarget"),
                      "pt_prior": r.get("priorPriceTarget")} for idx, r in ud.head(80).iterrows()] if ud is not None and len(ud) else []
        except Exception:
            moves = []
        return analyst_consensus(info, rows, targets, actions=analyst_actions(moves, ccy=(info.get("currency") or "USD")))
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
    # after run #29: the number says where it sits; the scale itself is
    # defined once, in the caption under the graphic (explain_consensus)
    who = (f"the analyst consensus ({consensus['mean']:g}, {_where(consensus['mean'])}"
           + (f"; {consensus['n']} analysts" if consensus.get("n") else "") + ")")
    diff = p - a
    if abs(diff) <= 0.125:
        return f"The debate's lean is in line with {who}."
    side = "constructive" if diff > 0 else "cautious"
    much = "much " if abs(diff) >= 0.5 else ""
    return f"The debate leans {much}more {side} than {who}."



def _where(mean) -> str:
    """'between strong buy and buy' / 'exactly hold' on the 1-5 scale."""
    import math
    m = float(mean)
    stops = {1: "strong buy", 2: "buy", 3: "hold", 4: "sell", 5: "strong sell"}
    if abs(m - round(m)) < 0.05:
        return f"exactly {stops[min(5, max(1, round(m)))]}"
    return f"between {stops[min(5, max(1, math.floor(m)))]} and {stops[min(5, max(1, math.ceil(m)))]}"


def explain_consensus(consensus) -> str:
    """PURE. What the analyst number means (after run #29: '1.38' alone
    read as nothing): where it sits on the provider's 1-5 scale, lower is
    more bullish."""
    if not consensus or consensus.get("mean") is None:
        return ""
    mean = float(consensus["mean"])
    where = _where(mean)
    n = consensus.get("n")
    who = f"the average of {n} analysts' ratings" if n else "the analysts' average rating"
    return f"{mean:g} is {who}, {where}, on a scale where 1 = strong buy, 2 = buy, 3 = hold, 4 = sell, 5 = strong sell"
