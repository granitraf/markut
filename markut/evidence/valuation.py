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


def pe_band(closes: list, annual_eps: list, years: int = 4) -> dict:
    """PURE. The company's own trailing GAAP P/E band: daily close over the
    latest annual diluted EPS on record at that date (a step function — EPS
    moves once a year), p25 / median / p75 over the last `years`. {} when
    there is not enough positive-EPS history. Labeled as what it is: a
    GAAP-trailing band used for SCALE, not a forecast."""
    if not closes or not annual_eps:
        return {}
    eps = sorted(annual_eps)   # (fy_end, eps) ascending
    cutoff = closes[-1][0][:4]
    cutoff = f"{int(cutoff) - years}{closes[-1][0][4:]}"
    ratios = []
    for d, c in closes:
        if d < cutoff:
            continue
        current = None
        for fy_end, e in eps:
            if fy_end <= d:
                current = e
        if current and current > 0 and c > 0:
            ratios.append(c / current)
    if len(ratios) < 120:
        return {}
    ratios.sort()
    q = lambda p: ratios[min(len(ratios) - 1, int(p * (len(ratios) - 1)))]
    return {"p25": round(q(0.25), 1), "median": round(q(0.5), 1), "p75": round(q(0.75), 1), "sessions": len(ratios)}


EPS_CASES = (("-15%", 0.85), ("consensus", 1.0), ("+10%", 1.10))
BAND_MAX_DISPERSION = 2.0   # p75/p25 above this and the historical band is information, not the grid's multiple range


def format_valuation_lines(info: dict, estimates: dict, current_fy=None, band_inputs: dict = None) -> list:
    """PURE. info = yfinance info; estimates = {"0y": {"avg":..}, "+1y": {...}};
    band_inputs = {"closes": [(date, close)], "annual_eps": [(fy_end, eps)]}.
    AUDIT (run #9, item 1): P/E on EACH fiscal year's consensus side by side;
    the scenario grid crosses EPS cases (-15% / consensus / +10%) with a
    multiple band from the company's own history, so no cell equals today's
    price by construction. The old 'today's next-FY multiple × current-FY
    EPS' rows are gone — they only measured growth already priced in."""
    lines = ["[VALUATION] (computed by code from the figures above — cite these rows; do not compute your own implied prices)"]
    price = _num(info.get("currentPrice"))

    def add(label, value, source="yfinance/info"):
        if value is not None:
            lines.append(f"- {label}: {value}  [source: {source}]")

    add("EV/EBITDA (TTM)", fmt_ratio(_num(info.get("enterpriseToEbitda"))))
    mc, fcf = _num(info.get("marketCap")), _num(info.get("freeCashflow"))
    if mc and fcf is not None:
        add("FCF yield (TTM FCF / market cap)", fmt_pct(fcf / mc), "computed")
    add("PEG (trailing, yfinance; growth basis not disclosed by the source)", fmt_ratio(_num(info.get("trailingPegRatio") or info.get("pegRatio"))))

    eps0 = _num(((estimates or {}).get("0y") or {}).get("avg"))
    eps1 = _num(((estimates or {}).get("+1y") or {}).get("avg"))
    fy0 = f" FY{int(current_fy)}" if current_fy else ""
    fy1 = f" FY{int(current_fy) + 1}" if current_fy else ""
    if price and eps0:
        add(f"P/E on current-FY{fy0} consensus EPS ({fmt_price(eps0)}, non-GAAP)", f"{price / eps0:.2f}x", "computed")
    if price and eps1:
        add(f"P/E on next-FY{fy1} consensus EPS ({fmt_price(eps1)}, non-GAAP)", f"{price / eps1:.2f}x", "computed")

    band = pe_band((band_inputs or {}).get("closes") or [], (band_inputs or {}).get("annual_eps") or [])
    own_fwd = _num(info.get("forwardPE"))
    if band:
        lines.append(f"- Trailing P/E band (company's own history: ~4y of daily closes / annual diluted GAAP EPS, {band['sessions']} sessions): "
                     f"p25 {band['p25']}x | median {band['median']}x | p75 {band['p75']}x  [source: computed]")
    # DISPERSION GUARD (the audit's peer-dispersion rule, applied to the company's
    # own history): a band whose p75 is more than 2x its p25 is not a usable
    # multiple range — an acquisition year or an earnings trough inflates GAAP
    # trailing P/E (AVGO live: p25 32.8x, p75 125.3x). Such a band is shown as
    # information only and the grid falls back to today's forward multiple ±20%.
    usable_band = bool(band) and band["p25"] > 0 and band["p75"] / band["p25"] <= BAND_MAX_DISPERSION
    if band and not usable_band:
        lines.append(f"- Band note: p75/p25 = {band['p75'] / band['p25']:.1f}x exceeds {BAND_MAX_DISPERSION:g}x — GAAP trailing history is too "
                     "dispersed to serve as a multiple range (acquisition or earnings-trough years); the grid below uses today's forward multiple instead  [source: computed]")
    if usable_band:
        mults = [("p25", band["p25"]), ("median", band["median"]), ("p75", band["p75"])]
        basis_note = "multiples are the company's GAAP-trailing history; EPS cases are non-GAAP consensus — a cross-basis table, for scale only, not forecasts"
    elif own_fwd:
        mults = [("0.8x fwd", round(own_fwd * 0.8, 1)), ("fwd", round(own_fwd, 1)), ("1.2x fwd", round(own_fwd * 1.2, 1))]
        basis_note = ("multiple history unusable or unavailable — multiples are today's forward P/E ±20%, so the consensus × fwd cell "
                      "equals today's price by construction; read the other eight cells for sensitivity")
    else:
        mults = []
    if price and eps1 and mults:
        lines.append(f"- Scenario grid (rows: next-FY{fy1} consensus EPS cases; columns: multiples; cell = implied price, % vs price {fmt_price(price)}; "
                     f"{basis_note})  [source: computed]")
        for case_label, factor in EPS_CASES:
            eps = eps1 * factor
            cells = " | ".join(f"{name} {m}x = {fmt_price(eps * m)} ({(eps * m / price - 1) * 100:+.1f}%)" for name, m in mults)
            lines.append(f"- Implied price, EPS {case_label} {fmt_price(eps)}: {cells}  [source: computed]")
    return lines if len(lines) > 1 else []
