"""Deterministic valuation block (AUDIT FIX run #2, item 10): multiples the
agents READ but never compute — EV/EBITDA, FCF yield, PEG, and a
bear/base/bull table of consensus EPS × multiple → implied price. The judge is
instructed to cite this table instead of doing its own arithmetic; the
governor's scenario check polices the rest.

No peer comparison, on purpose: peers need a curated list per ticker, and a
list only covers the symbols someone thought of (ask for CAKE and it is
empty). Everything here comes from the company's own data and is PURE."""
from markut.evidence.market import fmt_price, fmt_ratio, fmt_pct

SCENARIO_MULTIPLE_SPREAD = 0.20   # bear/bull multiples sit ±20% around today's forward P/E


def _num(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def format_valuation_lines(info: dict, estimates: dict, current_fy=None) -> list:
    """PURE. info = yfinance info; estimates = {"0y": {"avg":..}, "+1y": {...}}.
    Returns packet lines."""
    lines = ["[VALUATION] (computed by code from the figures above — cite these rows; do not compute your own implied prices)"]
    price = _num(info.get("currentPrice"))

    def add(label, value, source="yfinance/info"):
        if value is not None:
            lines.append(f"- {label}: {value}  [source: {source}]")

    add("EV/EBITDA (TTM)", fmt_ratio(_num(info.get("enterpriseToEbitda"))))
    mc, fcf = _num(info.get("marketCap")), _num(info.get("freeCashflow"))
    if mc and fcf is not None:
        add("FCF yield (TTM FCF / market cap)", fmt_pct(fcf / mc), "computed")
    add("PEG (trailing, yfinance)", fmt_ratio(_num(info.get("trailingPegRatio") or info.get("pegRatio"))))

    own_fwd = _num(info.get("forwardPE"))

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
    return lines if len(lines) > 1 else []
