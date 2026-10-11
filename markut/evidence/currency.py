"""Currencies and per-share basis (run #28, TSM — 2026-10-11).

WHY: the market packet assumed the trading currency and the reporting
currency were the same. TSM trades in USD (an ADR) and reports in TWD, so TWD
statement figures were printed with "$" and divided by the USD price: a 47.84%
FCF yield, a 0.5x-1.2x P/E band, "$333.05" EPS. Every statement figure now
carries its reporting currency's ISO code (never "$" unless it IS USD),
ratios that mix the two currencies are computed only after converting at a
stated exchange rate, and provider ratios that mix them silently are left
out with the reason said. A USD company prints exactly as before.
"""
from datetime import datetime


def currency_context(info: dict, fx: float = None, fx_date: str = None, fx_source: str = None) -> dict:
    """PURE. {"quote", "report", "mixed", "fx", "fx_date", "fx_source"} — fx
    converts ONE unit of the reporting currency into the trading currency
    (TWD -> USD: 0.03133). A missing financialCurrency means the provider did
    not say, and the figures are taken as quote-currency figures."""
    info = info or {}
    quote = (info.get("currency") or "USD").upper()
    report = (info.get("financialCurrency") or quote).upper()
    mixed = report != quote
    return {"quote": quote, "report": report, "mixed": mixed,
            "fx": (float(fx) if fx else None) if mixed else 1.0,
            "fx_date": fx_date if mixed else None,
            "fx_source": (fx_source or f"yfinance {report}{quote}=X close") if mixed and fx else None}


def money(n, ccy: str = "USD"):
    """'$2.35T' for USD; 'TWD 3.81T', 'CAD 9.00B' otherwise — an ISO code is
    never ambiguous, '$' alone is (TWD, CAD, AUD, HKD all write '$')."""
    if n is None:
        return None
    n = float(n)
    sign = "-" if n < 0 else ""
    a = abs(n)
    for cutoff, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if a >= cutoff:
            body = f"{a / cutoff:.2f}{suffix}"
            break
    else:
        body = f"{a:,.0f}"
    return f"{sign}${body}" if (ccy or "USD").upper() == "USD" else f"{sign}{ccy.upper()} {body}"


def per_unit(n, ccy: str = "USD"):
    """A price or a per-share figure: '$13.76' / 'TWD 331.25'."""
    if n is None:
        return None
    return f"${float(n):,.2f}" if (ccy or "USD").upper() == "USD" else f"{ccy.upper()} {float(n):,.2f}"


def to_quote(n, ctx: dict):
    """A reporting-currency amount in the trading currency, or None when the
    two differ and there is no rate."""
    if n is None or not ctx:
        return n
    if not ctx.get("mixed"):
        return float(n)
    return float(n) * ctx["fx"] if ctx.get("fx") else None


def currency_note(ctx: dict) -> str:
    """The packet line that tells every agent which currency is which."""
    if not ctx or not ctx.get("mixed"):
        return ""
    rate = (f"ratios that mix them are computed after converting {ctx['report']} to {ctx['quote']} at {ctx['fx']:.5f} "
            f"({ctx.get('fx_source')}, {ctx.get('fx_date')})") if ctx.get("fx") else \
        (f"no exchange rate was available, so no ratio mixes the two currencies")
    return (f"- CURRENCY NOTE: the price, market cap and per-share market data are in {ctx['quote']} (the traded security); "
            f"the financial statements are in {ctx['report']} and are labelled {ctx['report']}, never '$'; {rate}. "
            f"Provider ratios that silently mix the two (Price/Book, EV/EBITDA, third-party ratios) are left out.")


def share_basis_ok(trailing_eps_quote, annual_eps_report, ctx: dict) -> tuple:
    """PURE. (ok, note): is the statement EPS on the same per-share basis as
    the traded unit? The provider's trailing EPS is per traded unit in the
    trading currency; the latest annual EPS converted with the rate must be
    the same order of magnitude (within 2x either way — a year of growth fits;
    an ADS holding 5 ordinary shares does not)."""
    if trailing_eps_quote is None or annual_eps_report is None or not trailing_eps_quote or trailing_eps_quote <= 0:
        return True, ""
    conv = to_quote(annual_eps_report, ctx)
    if conv is None:
        return False, f"no exchange rate to convert the {ctx.get('report')} EPS"
    ratio = conv / float(trailing_eps_quote)
    if 0.5 <= ratio <= 2.0:
        return True, ""
    return False, (f"the per-share basis of the statement EPS ({per_unit(annual_eps_report, ctx.get('report'))}, "
                   f"{per_unit(conv, ctx.get('quote'))} converted) does not match the traded unit's trailing EPS "
                   f"({per_unit(trailing_eps_quote, ctx.get('quote'))}) — likely ordinary shares vs depositary shares")


def normalize_estimates(rows: dict, info: dict, ctx: dict) -> tuple:
    """PURE. (rows, note): the analysts' EPS estimate table on the traded unit
    in the trading currency (2026-10-11). The provider states no currency for
    it: for some foreign filers it is the reporting currency, for others the
    trading one. The trailing EPS (per traded unit, trading currency) decides:
    an estimate within 0.2x-5x of it is on the same basis; one that fits only
    after conversion at the rate is converted (and the note says so); one that
    fits neither is left out (said so) — a forward P/E of 0.96 from yuan
    estimates on a dollar price is never shown. A loss, or no trailing EPS,
    cannot be judged this way and the table is kept as given. Close
    currencies (EUR, GBP vs USD) are within the band either way and are not
    told apart."""
    rows = rows or {}
    key = "0y" if "0y" in rows else "+1y" if "+1y" in rows else None
    try:
        est = float(rows[key]["avg"]) if key else None
        trail = float((info or {}).get("trailingEps"))
    except (TypeError, ValueError, KeyError):
        return rows, ""
    if est is None or est != est or not trail or trail <= 0 or est <= 0:
        return rows, ""
    lo, hi = 0.2, 5.0
    if lo <= est / trail <= hi:
        return rows, ""
    fx = ctx.get("fx") if ctx and ctx.get("mixed") else None
    if fx and lo <= est * fx / trail <= hi:
        scaled = {k: {c: (float(v) * fx if c in ("avg", "low", "high", "yearAgoEps") and v == v and v is not None else v)
                      for c, v in r.items()} for k, r in rows.items()}
        return scaled, (f"- CURRENCY NOTE: the analyst EPS estimates are in {ctx['report']} (consensus {per_unit(est, ctx['report'])} against a "
                        f"trailing EPS of {per_unit(trail, ctx['quote'])}); converted to {ctx['quote']} at {fx:.5f} before any multiple  "
                        f"[source: computed]")
    return {}, (f"- CURRENCY NOTE: analyst EPS estimates omitted — the consensus ({est:,.2f}) is {est / trail:.1f}x the trailing EPS of "
                f"{per_unit(trail, (ctx or {}).get('quote', 'USD'))}, so its currency or per-share basis does not match the traded unit  "
                f"[source: computed]")


def fetch_fx(report: str, quote: str) -> tuple:
    """(rate report->quote, date, source) from the free yfinance FX pair; the
    inverse pair as fallback. (None, None, None) when neither has a close."""
    if not report or not quote or report.upper() == quote.upper():
        return 1.0, None, None
    try:
        import yfinance as yf
        for pair, invert in ((f"{report}{quote}=X", False), (f"{quote}{report}=X", True)):
            h = yf.Ticker(pair).history(period="5d")
            if h is not None and len(h.index) and h["Close"].iloc[-1] == h["Close"].iloc[-1]:
                v = float(h["Close"].iloc[-1])
                if v > 0:
                    d = h.index[-1]
                    return (1.0 / v if invert else v), (d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10]), \
                        f"yfinance {pair} close" + (", inverted" if invert else "")
    except Exception as e:
        print(f"CURRENCY: exchange rate {report}->{quote} unavailable ({e})")
    return None, None, None


def quarter_end(info: dict) -> str:
    """The provider's most recent quarter end, as a date ('' when absent)."""
    try:
        ts = (info or {}).get("mostRecentQuarter")
        return datetime.utcfromtimestamp(float(ts)).strftime("%Y-%m-%d") if ts else ""
    except Exception:
        return ""
