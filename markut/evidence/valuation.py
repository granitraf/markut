"""Deterministic valuation block (AUDIT FIX run #2, item 10): multiples the
agents READ but never compute — EV/EBITDA, FCF yield, PEG, a peer comparison
table, and a bear/base/bull table of consensus EPS × multiple → implied price.
The judge is instructed to cite this table instead of doing its own
arithmetic; the governor's scenario check polices the rest.

Everything here is pure except fetch_peer_metrics (yfinance, fail-soft)."""
import statistics

from markut import config
from markut.evidence.market import fmt_price, fmt_ratio, fmt_pct

PEER_FIELDS = ("forwardPE", "trailingPE", "enterpriseToEbitda", "priceToSalesTrailing12Months")
SCENARIO_MULTIPLE_SPREAD = 0.20   # bear/bull multiples sit ±20% around today's forward P/E


def fetch_peer_metrics(peers: list) -> dict:
    # yfinance per peer; one broken peer costs one row, never the block
    import yfinance as yf
    out = {}
    for p in peers or []:
        try:
            info = yf.Ticker(p).info or {}
            out[p] = {k: info.get(k) for k in PEER_FIELDS}
        except Exception as e:
            out[p] = {"error": str(e)}
    return out


def _num(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def format_valuation_lines(info: dict, estimates: dict, peers: dict, current_fy=None) -> list:
    """PURE. info = yfinance info; estimates = {"0y": {"avg":..}, "+1y": {...}};
    peers = {"NVDA": {forwardPE:.., ...}, ...}. Returns packet lines."""
    lines = ["[VALUATION] (computed by code from the figures above — cite these rows; do not compute your own implied prices)"]
    price = _num(info.get("currentPrice"))

    def add(label, value, source="yfinance/info"):
        if value is not None:
            lines.append(f"- {label}: {value}  [source: {source}]")

    add("EV/EBITDA (TTM)", fmt_ratio(_num(info.get("enterpriseToEbitda"))))
    add("EV/Revenue (TTM)", fmt_ratio(_num(info.get("enterpriseToRevenue"))))
    mc, fcf = _num(info.get("marketCap")), _num(info.get("freeCashflow"))
    if mc and fcf is not None:
        add("FCF yield (TTM FCF / market cap)", fmt_pct(fcf / mc), "computed")
    add("PEG (trailing, yfinance)", fmt_ratio(_num(info.get("trailingPegRatio") or info.get("pegRatio"))))

    # ---- peer table ----
    fwd_pes = []
    rows = []
    for sym, m in (peers or {}).items():
        if not isinstance(m, dict) or m.get("error"):
            continue
        parts = []
        for key, label in (("forwardPE", "fwd P/E"), ("trailingPE", "trailing P/E"),
                           ("enterpriseToEbitda", "EV/EBITDA"), ("priceToSalesTrailing12Months", "P/S")):
            v = _num(m.get(key))
            if v is not None:
                parts.append(f"{label} {v:.1f}")
        if _num(m.get("forwardPE")) is not None:
            fwd_pes.append(_num(m["forwardPE"]))
        if parts:
            rows.append(f"- Peer {sym}: " + " | ".join(parts) + "  [source: yfinance/info]")
    own_fwd = _num(info.get("forwardPE"))
    if rows:
        own = []
        for key, label in (("forwardPE", "fwd P/E"), ("trailingPE", "trailing P/E"),
                           ("enterpriseToEbitda", "EV/EBITDA"), ("priceToSalesTrailing12Months", "P/S")):
            v = _num(info.get(key))
            if v is not None:
                own.append(f"{label} {v:.1f}")
        lines.append("- Peer comparison (same yfinance fields; forward P/E on non-GAAP consensus):")
        if own:
            lines.append("- This company: " + " | ".join(own) + "  [source: yfinance/info]")
        lines.extend(rows)
    peer_median = statistics.median(fwd_pes) if fwd_pes else None
    if peer_median is not None:
        add("Peer median forward P/E", f"{peer_median:.1f}", "computed")

    # ---- scenario table: consensus EPS × multiple → implied price ----
    if own_fwd and price:
        bear_m, base_m, bull_m = own_fwd * (1 - SCENARIO_MULTIPLE_SPREAD), own_fwd, own_fwd * (1 + SCENARIO_MULTIPLE_SPREAD)
        lines.append(f"- Scenario multiples (mechanical: today's forward P/E {own_fwd:.1f}x ±{int(SCENARIO_MULTIPLE_SPREAD*100)}%; "
                     f"bear {bear_m:.1f}x / base {base_m:.1f}x / bull {bull_m:.1f}x — not forecasts)  [source: computed]")
        for key, label in (("0y", "current-FY"), ("+1y", "next-FY")):
            eps = _num(((estimates or {}).get(key) or {}).get("avg"))
            if eps is None:
                continue
            fy = f" FY{int(current_fy) + (1 if key == '+1y' else 0)}" if current_fy else ""
            cells = []
            for name, m in (("bear", bear_m), ("base", base_m), ("bull", bull_m)):
                implied = eps * m
                cells.append(f"{name} {m:.1f}x = {fmt_price(implied)} ({(implied / price - 1) * 100:+.1f}% vs price)")
            lines.append(f"- Implied price from {label}{fy} consensus EPS {fmt_price(eps)} (non-GAAP): " + " | ".join(cells) + "  [source: computed]")
            if peer_median is not None:
                implied = eps * peer_median
                lines.append(f"- Implied price from {label}{fy} EPS at the peer median {peer_median:.1f}x: {fmt_price(implied)} "
                             f"({(implied / price - 1) * 100:+.1f}% vs price)  [source: computed]")
    return lines if len(lines) > 1 else []
