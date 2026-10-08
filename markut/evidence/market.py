"""Market-data baseline: yfinance owns the packet, FMP contributes DCF and
a few valuation ratios. Moved verbatim from the notebook (cells 10/12);
the only edit is the FMP key now coming from markut.config."""
import requests

from markut import config

FMP_BASE = "https://financialmodelingprep.com/stable"


def fmp_get_json(endpoint: str, symbol: str, **extra_params):
    key = config.FMP_API_KEY
    params = {"symbol": symbol, "apikey": key, **extra_params}
    response = requests.get(f"{FMP_BASE}/{endpoint}", params=params, timeout=15)
    return response.json()


import yfinance as yf
from datetime import datetime

# ---------- pure formatting helpers (no network) ----------
# WHY: the debate agents read this packet as plain text. Raw values like
# 4785585180000 or 0.7414 are easy for a model to mis-scale (billions vs
# trillions, fraction vs percent). Formatting up front ("$4.79T", "74.14%")
# removes a whole class of unit-confusion errors before they can happen.
# All four are None-safe: they return None for missing input so add_line's
# skip-None convention keeps working after formatting.

def fmt_price(n):
    return None if n is None else f"${float(n):,.2f}"

def fmt_big(n):
    # Market caps / revenue / FCF as $X.XXT, $X.XXB, $X.XXM.
    if n is None:
        return None
    n = float(n)
    sign = "-" if n < 0 else ""
    a = abs(n)
    for cutoff, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if a >= cutoff:
            return f"{sign}${a / cutoff:.2f}{suffix}"
    return f"{sign}${a:,.0f}"

def fmt_pct(x):
    # yfinance reports margins/growth/ROE as fractions (0.7414 -> "74.14%").
    return None if x is None else f"{float(x) * 100:.2f}%"

def fmt_ratio(n):
    # P/E, P/B, D/E — plain 2-decimal numbers, no unit.
    return None if n is None else f"{float(n):.2f}"


def format_market_lines(info: dict) -> list:
    # WHY this helper exists (extracted for the testing pass): formatting and
    # network fetching used to live in one function, which made the formatting
    # logic UNTESTABLE without live calls. This function is PURE — it takes an
    # already-fetched info dict and returns tagged lines — so tests can feed it
    # a fake dict and verify every branch offline, for free. The fetch/format
    # seam is exactly where data bugs (missing keys, wrong scaling) hide.
    lines = []

    def add(label, value, source="yfinance/info"):
        # Same skip-None convention as everywhere else in the notebook.
        if value is not None:
            lines.append(f"- {label}: {value}  [source: {source}]")

    # WHY the period labels on every line: trailing vs forward answer different
    # questions (what happened vs what the market expects). Labeling each value's
    # period stops the bull/bear from quoting a forward estimate as if it were a
    # historical fact — a subtle hallucination the old packet couldn't prevent.
    lines.append("[QUOTE & VALUATION]")
    add("Price (current)", fmt_price(info.get("currentPrice")))
    add("Market cap", fmt_big(info.get("marketCap")))
    lo, hi = info.get("fiftyTwoWeekLow"), info.get("fiftyTwoWeekHigh")
    if lo is not None and hi is not None:
        add("52-week range", f"{fmt_price(lo)} / {fmt_price(hi)}")
    # AUDIT FIX (run #2): every EPS and P/E line now names its PERIOD and its
    # ACCOUNTING BASIS. yfinance's trailingEps is GAAP TTM; forwardEps is the
    # analyst consensus for a FUTURE fiscal year and is non-GAAP for most
    # companies. Unlabeled, the pair reads like "earnings will double" when it
    # is really GAAP-vs-non-GAAP across different years.
    add("P/E (trailing TTM, GAAP EPS)", fmt_ratio(info.get("trailingPE")))
    add("P/E (forward, consensus non-GAAP EPS)", fmt_ratio(info.get("forwardPE")))
    add("Price/Book (mrq)", fmt_ratio(info.get("priceToBook")))
    add("EPS (trailing TTM, GAAP)", fmt_price(info.get("trailingEps")))
    add("EPS (forward, consensus non-GAAP, next 12 months)", fmt_price(info.get("forwardEps")))

    lines.append("\n[FUNDAMENTALS]")
    add("Profit margin (TTM)", fmt_pct(info.get("profitMargins")))
    add("Gross margin (TTM)", fmt_pct(info.get("grossMargins")))
    add("Return on equity (TTM)", fmt_pct(info.get("returnOnEquity")))
    # WHY no fmt_pct here: yfinance reports debtToEquity ALREADY scaled as a
    # percent-style number (e.g. 12.95 means 12.95%). Multiplying by 100 again
    # would print a wildly wrong figure the agents would happily argue about.
    add("Debt/Equity (mrq, %)", fmt_ratio(info.get("debtToEquity")))
    add("Free cash flow (TTM)", fmt_big(info.get("freeCashflow")))
    # AUDIT FIX (run #2): yfinance revenueGrowth is the MOST RECENT QUARTER vs
    # the same quarter a year earlier — NOT annual growth. Labeled "(yoy)" it
    # was quoted as "revenue grew 85.5% in FY2025". The annual figure is
    # computed separately from the income statement (fy_growth_lines).
    add("Revenue growth (MRQ YoY, most recent quarter vs year-ago quarter)", fmt_pct(info.get("revenueGrowth")))
    add("Earnings growth (MRQ YoY)", fmt_pct(info.get("earningsGrowth")))
    return lines


def fy_growth_lines(annuals: list) -> list:
    # PURE. annuals = [(fiscal_year, revenue, net_income), ...] newest first,
    # straight from income_stmt columns. Emits full-year growth with BOTH years
    # named, so an annual growth claim has an annual anchor to trace to.
    lines = []
    if len(annuals) < 2:
        return lines
    (fy1, rev1, ni1), (fy0, rev0, ni0) = annuals[0], annuals[1]
    def growth(a, b):
        try:
            if a is None or b is None or float(b) == 0 or a != a or b != b:
                return None
            return f"{(float(a) / float(b) - 1) * 100:+.2f}%"
        except (TypeError, ValueError):
            return None
    g = growth(rev1, rev0)
    if g is not None:
        lines.append(f"- Revenue growth (FY{fy1} vs FY{fy0}, annual): {g}  [source: yfinance/income_stmt]")
    g = growth(ni1, ni0)
    if g is not None:
        lines.append(f"- Net income growth (FY{fy1} vs FY{fy0}, annual): {g}  [source: yfinance/income_stmt]")
    return lines


def format_estimate_lines(estimates: dict, current_fy=None) -> list:
    # PURE. estimates = {"0y": {"avg": 7.9, "numberOfAnalysts": 30}, "+1y": {...}}
    # (yfinance earnings_estimate rows). Consensus EPS is non-GAAP for most
    # issuers; each line says so and names the fiscal year it is for, so a
    # reader can never mistake "next FY consensus" for a trailing GAAP figure.
    lines = []
    for key, label in (("0y", "current FY"), ("+1y", "next FY")):
        row = estimates.get(key) or {}
        avg = row.get("avg")
        if avg is None or avg != avg:
            continue
        fy = ""
        if current_fy:
            fy = f"FY{int(current_fy) + (1 if key == '+1y' else 0)}, "
        n = row.get("numberOfAnalysts")
        n_txt = f", {int(n)} analysts" if n is not None and n == n else ""
        lines.append(f"- EPS consensus ({fy}{label}, non-GAAP{n_txt}): {fmt_price(avg)}  [source: yfinance/earnings_estimate]")
    return lines


def basis_notes(info: dict) -> list:
    # PURE. Packet-level warnings whenever a prompt-visible pair mixes GAAP with
    # non-GAAP or mixes periods. The agents read these lines like any other
    # evidence, and the governor's label check enforces them downstream.
    notes = []
    if info.get("trailingEps") is not None and info.get("forwardEps") is not None:
        notes.append("- BASIS NOTE: trailing EPS is GAAP (TTM); forward EPS is non-GAAP analyst consensus for a future period. "
                     "Their ratio is NOT an earnings growth rate, and trailing P/E vs forward P/E are not on the same basis.")
    if info.get("revenueGrowth") is not None:
        notes.append("- PERIOD NOTE: 'Revenue growth (MRQ YoY)' is one quarter against the year-ago quarter, not a fiscal-year rate; "
                     "annual growth is the separate 'FY vs FY' line.")
    return notes


def market_snapshot(ticker: str) -> str:
    # WHY this function exists: yfinance replaces FMP as the evidence baseline
    # because it covers more of what the debate needs (forward estimates and analyst
    # recommendations) with no API key. FMP is kept ONLY for the DCF fair-value
    # estimate — the one input yfinance does not provide.
    symbol = ticker.upper().strip()
    t = yf.Ticker(symbol)  # constraint: use ONLY the Ticker interface
    out = []

    def add_line(label, value, source):
        # Same convention as fetch_fundamentals: skip None so the packet only
        # contains claims the agents are allowed to ground arguments in.
        if value is not None:
            out.append(f"- {label}: {value}  [source: {source}]")

    # Fetch t.info ONCE up front. WHY: four of the five sections read from it, and
    # accessing it can trigger a network call — fetching once is cheaper and gives
    # every section a consistent snapshot. On failure we fall back to {} so the
    # .get() reads below all return None and their lines are skipped, instead of
    # the whole packet dying on one broken endpoint.
    try:
        info = t.info or {}
    except Exception as e:
        info = {}
        out.append(f"[info unavailable: {e}]")

    # ---- 1 & 2. QUOTE & VALUATION + FUNDAMENTALS (info-derived, pure part) ----
    try:
        out.extend(format_market_lines(info))
    except Exception as e:
        out.append(f"[quote/fundamentals sections unavailable: {e}]")

    # Most recent ANNUAL revenue / net income from the income statement.
    # WHY a separate try: t.income_stmt is its own network call. If it breaks we
    # lose two lines, not the whole fundamentals section — one broken yfinance
    # property must cost one section, never the whole packet.
    try:
        inc = t.income_stmt
        if inc is not None and len(inc.columns) > 0:
            col = inc.columns[0]  # yfinance orders columns newest-first
            fy = getattr(col, "year", col)  # label the fiscal year explicitly
            for row, nice in (("Total Revenue", "Revenue"), ("Net Income", "Net income")):
                if row in inc.index:
                    val = inc.loc[row, col]
                    if val == val:  # NaN check without importing pandas (NaN != NaN)
                        add_line(f"{nice} (FY{fy} annual)", fmt_big(val), "yfinance/income_stmt")
            # annual growth, both years named (AUDIT FIX: the MRQ figure above
            # must never be the only growth number in the packet)
            annuals = []
            for c in list(inc.columns)[:2]:
                annuals.append((getattr(c, "year", c),
                                inc.loc["Total Revenue", c] if "Total Revenue" in inc.index else None,
                                inc.loc["Net Income", c] if "Net Income" in inc.index else None))
            out.extend(fy_growth_lines(annuals))
    except Exception as e:
        out.append(f"[income_stmt unavailable: {e}]")

    # Consensus EPS by fiscal year (non-GAAP), separately from trailing GAAP EPS.
    try:
        est = t.earnings_estimate
        if est is not None and len(est.index) > 0:
            rows = {str(k): {c: est.loc[k, c] for c in est.columns} for k in est.index}
            current_fy = None
            try:
                nfe = info.get("nextFiscalYearEnd")
                if nfe:
                    current_fy = datetime.fromtimestamp(float(nfe)).year
            except Exception:
                current_fy = None
            out.extend(format_estimate_lines(rows, current_fy))
    except Exception as e:
        out.append(f"[earnings_estimate unavailable: {e}]")
    out.extend(basis_notes(info))

    # FMP adds intraday context yfinance/info does not expose in the current packet.
    try:
        quote = fmp_get_json("quote", symbol)
        if isinstance(quote, list) and quote:
            d = quote[0]
            day_low, day_high = d.get("dayLow"), d.get("dayHigh")
            if day_low is not None and day_high is not None:
                add_line("Day range", f"{fmt_price(day_low)} / {fmt_price(day_high)}", "FMP/quote")
        elif isinstance(quote, dict):
            out.append(f"[quote response: {quote}]")
    except Exception as e:
        out.append(f"[quote section unavailable: {e}]")

    # FMP adds latest fiscal-year EPS labels to distinguish them from yfinance TTM/forward EPS.
    try:
        income = fmp_get_json("income-statement", symbol, limit=1)
        if isinstance(income, list) and income:
            d = income[0]
            fiscal_year = d.get("fiscalYear") or d.get("date")
            suffix = f" ({fiscal_year})" if fiscal_year is not None else ""
            add_line(f"EPS (latest fiscal year{suffix})", fmt_price(d.get("eps")), "FMP/income-statement")
            add_line(f"EPS diluted (latest fiscal year{suffix})", fmt_price(d.get("epsDiluted")), "FMP/income-statement")
        elif isinstance(income, dict):
            out.append(f"[income-statement response: {income}]")
    except Exception as e:
        out.append(f"[income-statement section unavailable: {e}]")

    # FMP adds valuation efficiency ratios yfinance does not expose cleanly.
    try:
        ratios = fmp_get_json("ratios-ttm", symbol)
        if isinstance(ratios, list) and ratios:
            d = ratios[0]
            add_line("Price/Sales (TTM)", fmt_ratio(d.get("priceToSalesRatioTTM")), "FMP/ratios")
            add_line("Price/FCF (TTM)", fmt_ratio(d.get("priceToFreeCashFlowRatioTTM")), "FMP/ratios")
            add_line("Free cash flow yield (TTM)", fmt_pct(d.get("freeCashFlowYieldTTM")), "FMP/ratios")
        elif isinstance(ratios, dict):
            out.append(f"[ratios response: {ratios}]")
    except Exception as e:
        out.append(f"[ratios section unavailable: {e}]")

    # ---- 3. ANALYST VIEW ----
    try:
        out.append("\n[ANALYST VIEW]")
        apt = t.analyst_price_targets or {}
        add_line("Price target (mean)", fmt_price(apt.get("mean")), "yfinance/analyst_price_targets")
        th, tl = apt.get("high"), apt.get("low")
        if th is not None and tl is not None:
            add_line("Price target (high / low)", f"{fmt_price(th)} / {fmt_price(tl)}",
                     "yfinance/analyst_price_targets")
        add_line("Recommendation", info.get("recommendationKey"), "yfinance/info")
        add_line("Analyst count", info.get("numberOfAnalystOpinions"), "yfinance/info")
    except Exception as e:
        out.append(f"[analyst section unavailable: {e}]")

    # ---- 4. EVENTS ----
    try:
        ed = t.earnings_dates
        next_dt = None
        if ed is not None and len(ed.index) > 0:
            for ts in ed.index:
                dt = ts.to_pydatetime()
                now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
                if dt > now and (next_dt is None or dt < next_dt):
                    next_dt = dt
        if next_dt is not None:
            out.append("\n[EVENTS]")
            add_line("Next earnings date", next_dt.strftime("%Y-%m-%d"), "yfinance/earnings_dates")
    except Exception as e:
        out.append(f"[events section unavailable: {e}]")

    # ---- 5. DCF (the one FMP holdover) ----
    try:
        out.append("\n[DCF]")
        dcf = fmp_get_json("discounted-cash-flow", symbol)
        if isinstance(dcf, list) and dcf:
            d = dcf[0]
            dcf_val = d.get("dcf")
            add_line("DCF fair value", fmt_price(dcf_val), "FMP/dcf")
            price = info.get("currentPrice")
            if dcf_val is not None and price is not None:
                # WHY spell out the direction: leaving "247.17 vs 197.58" as raw
                # numbers forces each agent to do the comparison itself; stating
                # ABOVE/BELOW with the % gap removes an arithmetic step a model
                # could botch mid-argument.
                gap = (float(dcf_val) / float(price) - 1) * 100
                direction = "ABOVE" if float(dcf_val) > float(price) else "BELOW"
                add_line("DCF vs price",
                         f"fair value is {direction} current price ({gap:+.1f}%)",
                         "FMP/dcf")
        elif isinstance(dcf, dict):
            out.append(f"[dcf response: {dcf}]")
    except Exception as e:
        out.append(f"[dcf section unavailable: {e}]")

    return "\n".join(out) if out else "[No data returned — check ticker/network]"
