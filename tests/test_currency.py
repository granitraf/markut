"""P0 (run #28, TSM): every figure carries its currency and per-share basis.

TSM trades in USD (ADR) and reports in TWD. The packet printed TWD statement
figures with "$" and divided them by the USD price: a 47.84% FCF yield, a
0.5x-1.2x P/E band, $333.05 EPS. Rules checked here on an AAPL-shaped USD
company (unchanged output), a TSM-shaped ADR (USD quote, TWD statements) and a
CAD reporter quoted in USD, plus the no-exchange-rate case. Offline."""
import pandas as pd

from markut.evidence import currency, edgar, market, valuation


def check(label, condition):
    assert condition, label


FX_TWD = 0.03133          # TWD -> USD (yfinance TWDUSD=X, 2026-10-09)


def _tsm_info(fx=FX_TWD):
    info = {"currency": "USD", "financialCurrency": "TWD", "currentPrice": 453.31, "marketCap": 2.35e12, "trailingPE": 32.94,
            "trailingEps": 13.76, "priceToBook": 93.33, "enterpriseToEbitda": 5.178, "debtToEquity": 16.5,
            "revenueGrowth": 0.36, "earningsGrowth": 0.774, "mostRecentQuarter": 1782777600,
            "_fcf_quarters": {"fcf": 1.12e12, "ocf": 2.63e12, "capex": -1.51e12, "quarters": ["2026-06-30"]},
            "_fwd_eps_canonical": {"value": 22.05, "source": "yfinance/earnings_estimate", "analysts": 13}}
    info["_ccy"] = currency.currency_context(info, fx=fx, fx_date="2026-10-09" if fx else None)
    return info


def _aapl_info():
    info = {"currency": "USD", "financialCurrency": "USD", "currentPrice": 336.64, "marketCap": 4.91e12, "trailingPE": 38.61,
            "trailingEps": 8.72, "priceToBook": 45.74, "enterpriseToEbitda": 28.4, "debtToEquity": 78.44, "revenueGrowth": 0.164,
            "mostRecentQuarter": 1782518400,
            "_fcf_quarters": {"fcf": 136.68e9, "ocf": 150.0e9, "capex": -13.32e9, "quarters": ["2026-06-27"]},
            "_fwd_eps_canonical": {"value": 9.58, "source": "yfinance/earnings_estimate", "analysts": 40}}
    info["_ccy"] = currency.currency_context(info)
    return info


def test_each_market_figure_carries_its_currency():
    t = "\n".join(market.format_market_lines(_tsm_info()))
    check("TSM-shaped: market cap stays in the trading currency", "Market cap: $2.35T" in t)
    check(f"TSM-shaped: free cash flow is labelled TWD, never '$' ({[l for l in t.splitlines() if 'Free cash flow' in l]})",
          "TWD 1.12T" in t and "TWD 2.63T" in t and "$1.12T" not in t)
    check("TSM-shaped: a currency note names both currencies and the rate",
          "CURRENCY NOTE" in t and "TWD" in t and "USD" in t and "0.03133" in t)
    check("TSM-shaped: the provider's Price/Book (book in TWD, price per ADR in USD) is not printed", "93.33" not in t)
    a = "\n".join(market.format_market_lines(_aapl_info()))
    check("AAPL-shaped: USD company unchanged — '$' figures, no currency note",
          "Market cap: $4.91T" in a and "$136.68B" in a and "CURRENCY NOTE" not in a and "Price/Book (mrq): 45.74" in a)


def test_latest_quarter_growth_names_its_quarter():
    t = "\n".join(market.format_market_lines(_tsm_info()))
    check(f"TSM-shaped: the MRQ growth line says which quarter ({[l for l in t.splitlines() if 'Revenue growth (MRQ' in l]})",
          "quarter ended 2026-06-30" in t)


def test_mixed_currency_ratios_are_converted_or_omitted():
    v = "\n".join(valuation.format_valuation_lines(_tsm_info(), {"+1y": {"avg": 22.05, "numberOfAnalysts": 13}}, 2026, {}))
    check(f"TSM-shaped: FCF yield converts TWD cash flow to USD first (1.12T x 0.03133 / 2.35T = 1.49%) ({[l for l in v.splitlines() if 'FCF yield' in l]})",
          "1.49%" in v and "47.66%" not in v and "0.03133" in v)
    check("TSM-shaped: EV/EBITDA from the provider (EV mixes currencies) is omitted, and the omission is stated",
          "5.18" not in v and "omitted" in v.lower())
    nofx = "\n".join(valuation.format_valuation_lines(_tsm_info(fx=None), {"+1y": {"avg": 22.05}}, 2026, {}))
    check(f"no exchange rate -> no cross-currency ratio, with the reason ({[l for l in nofx.splitlines() if 'FCF' in l or 'exchange' in l]})",
          "FCF yield" not in nofx or "omitted" in nofx.lower())
    check("no exchange rate -> the reason is stated", "no exchange rate" in nofx.lower())
    a = "\n".join(valuation.format_valuation_lines(_aapl_info(), {"+1y": {"avg": 9.58}}, 2026, {}))
    check("AAPL-shaped: unchanged — EV/EBITDA and FCF yield as before", "EV/EBITDA (TTM): 28.40" in a and "FCF yield (TTM FCF / market cap): 2.78%" in a)


def _closes(start=150.0, end=450.0, n=1000, last="2026-10-09"):
    days = pd.bdate_range(end=last, periods=n)
    return [(d.strftime("%Y-%m-%d"), start + (end - start) * i / (n - 1)) for i, d in enumerate(days)]


def test_pe_band_converts_eps_and_checks_the_share_basis():
    info = _tsm_info()
    per_ads = [("2022-12-31", 195.0), ("2023-12-31", 166.0), ("2024-12-31", 226.25), ("2025-12-31", 331.25)]   # TWD per ADS
    v = "\n".join(valuation.format_valuation_lines(info, {"+1y": {"avg": 22.05}}, 2026, {"closes": _closes(), "annual_eps": per_ads}))
    band = [l for l in v.splitlines() if "Trailing P/E band" in l]
    check(f"TSM-shaped: the band divides USD closes by EPS converted to USD — tens of x, not 0.5x ({band})",
          band and "p25 0." not in band[0] and "p25 1." not in band[0] and "converted" in band[0])
    per_share = [(d, e / 5) for d, e in per_ads]           # TWD per ordinary share: 5 shares per ADS
    v2 = "\n".join(valuation.format_valuation_lines(info, {"+1y": {"avg": 22.05}}, 2026, {"closes": _closes(), "annual_eps": per_share}))
    check(f"per-share basis that does not match the traded unit -> no band, with the reason ({[l for l in v2.splitlines() if 'band' in l.lower()]})",
          "Trailing P/E band (" not in v2 and "per-share basis" in v2)


def test_statement_lines_use_the_reporting_currency():
    inc = pd.DataFrame({pd.Timestamp("2025-12-31"): {"Total Revenue": 3.81e12, "Net Income": 1.70e12},
                        pd.Timestamp("2024-12-31"): {"Total Revenue": 2.89e12, "Net Income": 1.16e12}})
    t = "\n".join(market.statement_lines(inc, "TWD"))
    check(f"TSM-shaped: FY revenue and net income in TWD ({t})", "Revenue (FY2025 annual): TWD 3.81T" in t and "TWD 1.70T" in t and "$3.81T" not in t)
    check("AAPL-shaped: USD unchanged", "Revenue (FY2025 annual): $3.81T" in "\n".join(market.statement_lines(inc, "USD")))
    bs = pd.DataFrame({pd.Timestamp("2026-06-30"): {"Total Debt": 1.06e12, "Capital Lease Obligations": 33.28e9, "Stockholders Equity": 6.43e12}})
    lev = "\n".join(market.leverage_lines(bs, {"debtToEquity": 16.5}, ccy="TWD"))
    check(f"TSM-shaped: debt and equity amounts in TWD ({lev[:160]})", "TWD 1.06T" in lev and "TWD 6.43T" in lev and "$1.06T" not in lev)


def test_second_provider_figures_respect_currency():
    ctx = _tsm_info()["_ccy"]
    eps = "\n".join(market.fmp_income_lines({"fiscalYear": "2025", "eps": 333.05, "epsDiluted": 333.05, "reportedCurrency": "TWD"}, ctx))
    check(f"TSM-shaped: the second provider's EPS is labelled TWD ({eps})", "TWD 333.05" in eps and "$333.05" not in eps)
    ratios = "\n".join(market.fmp_ratio_lines({"priceToSalesRatioTTM": 14.89, "priceToFreeCashFlowRatioTTM": 59.12}, ctx))
    check(f"TSM-shaped: the second provider's cross-currency ratios are omitted with the reason ({ratios})",
          "14.89" not in ratios and "omitted" in ratios.lower())
    dcf = "\n".join(market.dcf_lines({"dcf": 130.18}, 453.31, ctx))
    check(f"TSM-shaped: a DCF of unknown currency/basis is not printed as a dollar value ({dcf})", "$130.18" not in dcf)
    usd = currency.currency_context({"currency": "USD", "financialCurrency": "USD"})
    check("AAPL-shaped: USD ratios still print", "Price/Sales (TTM): 14.89" in "\n".join(market.fmp_ratio_lines({"priceToSalesRatioTTM": 14.89}, usd)))


def test_canadian_dollar_reporter_quoted_in_usd():
    info = {"currency": "USD", "financialCurrency": "CAD", "currentPrice": 33.0, "marketCap": 69.0e9,
            "_fcf_quarters": {"fcf": 9.0e9, "ocf": 15.0e9, "capex": -6.0e9, "quarters": ["2026-06-30"]}}
    info["_ccy"] = currency.currency_context(info, fx=0.73, fx_date="2026-10-09")
    t = "\n".join(market.format_market_lines(info))
    v = "\n".join(valuation.format_valuation_lines(info, {}, 2026, {}))
    check("synthetic CAD reporter: FCF labelled CAD", "CAD 9.00B" in t)
    check(f"synthetic CAD reporter: FCF yield converted (9.0B x 0.73 / 69.0B = 9.52%) ({[l for l in v.splitlines() if 'FCF' in l]})", "9.52%" in v)


def test_release_tables_carry_their_currency(monkeypatch):
    html = ("<html><body><p>TSMC's 2026 second quarter consolidated results: (Unit: NT$ million, except for EPS)</p>"
            "<table><tr><td></td><td>2Q26</td><td>2Q25</td></tr>"
            "<tr><td>Net sales</td><td>1,270,381</td><td>933,792</td></tr>"
            "<tr><td>Gross profit</td><td>860,311</td><td>547,369</td></tr>"
            "<tr><td>Net income</td><td>706,562</td><td>398,273</td></tr>"
            "<tr><td>EPS (NT$)</td><td>27.25</td><td>15.36</td></tr></table></body></html>")
    rows = {r["label"]: r for r in edgar.parse_tables(html)}
    check(f"TSM-shaped: 'Unit: NT$ million' gives units and currency ({ {k: (r.get('units'), r.get('currency')) for k, r in rows.items()} })",
          rows["Net sales"]["units"] == "millions" and rows["Net sales"].get("currency") == "TWD")
    us = ("<html><body><p>CONDENSED CONSOLIDATED STATEMENTS OF OPERATIONS (In millions, except per-share amounts)</p>"
          "<table><tr><td></td><td>June 27, 2026</td><td>June 28, 2025</td></tr><tr><td>Total net sales</td><td>$ 109,417</td><td>$ 94,036</td></tr></table></body></html>")
    r = edgar.parse_tables(us)[0]
    check("AAPL-shaped: '(In millions …)' with $ cells stays USD millions", r["units"] == "millions" and r.get("currency") in ("USD", "", None))
    from markut.evidence import core
    monkeypatch.setattr(core, "core_sources", lambda fs: [{"form": "6-K", "name": "rel.htm", "filing_date": "2026-07-16", "url": "u",
                                                           "html": html, "text": "", "headline_period": "quarter"}])
    t = core.extract_core({}, {"segments": [], "company_kpis": []}, None)["text"]
    line = next((l for l in t.splitlines() if l.startswith("- Net sales") or "Revenue" in l and "1,270,381" in l), "")
    check(f"TSM-shaped: the core figure line names TWD ({line[:200]!r})", "TWD millions" in line)


def test_foreign_release_columns_and_statement_noise(monkeypatch):
    # TSM's real release table: "2Q26 | 2Q25 | YoY % | 1Q26 | QoQ %"; its
    # interim statements add an equity statement ("Net income" under
    # Unappropriated Earnings) and a subsidiaries table of ownership %
    release = ("<html><body><p>TSMC's 2026 second quarter consolidated results: (Unit: NT$ million, except for EPS)</p>"
               "<table><tr><td></td><td>2Q26 Amount a</td><td>2Q25 Amount</td><td>YoY Inc. (Dec.) %</td><td>1Q26 Amount</td><td>QoQ Inc. (Dec.) %</td></tr>"
               "<tr><td>Net sales</td><td>1,270,381</td><td>933,792</td><td>36.0</td><td>1,134,103</td><td>12.0</td></tr>"
               "<tr><td>Gross profit</td><td>860,311</td><td>547,369</td><td>57.2</td><td>751,295</td><td>14.5</td></tr>"
               "<tr><td>Net income</td><td>706,562</td><td>398,273</td><td>77.4</td><td>572,480</td><td>23.4</td></tr></table>"
               "<p>BALANCE, JANUARY 1, 2026</p>"
               "<table><tr><td></td><td>Capital Stock</td><td>Unappropriated Earnings</td><td>Total Equity</td></tr>"
               "<tr><td>Net income</td><td>-</td><td>759,837,230</td><td>759,837,230</td></tr>"
               "<tr><td>Cash dividends</td><td>-</td><td>(155,580,000)</td><td>(155,580,000)</td></tr></table>"
               "<p>Subsidiaries included in the consolidated financial statements</p>"
               "<table><tr><td>Name of Subsidiary</td><td>Percentage of Ownership June 30, 2026</td><td>Percentage of Ownership December 31, 2025</td></tr>"
               "<tr><td>TSMC Japan Limited</td><td>100%</td><td>100%</td></tr><tr><td>TSMC Europe B.V.</td><td>100%</td><td>100%</td></tr></table>"
               "<p>The fair value of the restricted stock awards was estimated with the following assumptions:</p>"
               "<table><tr><td></td><td>2024 RSAs September 1, 2024</td></tr><tr><td>Risk-free interest rate</td><td>1.40%</td></tr>"
               "<tr><td>Expected volatility</td><td>25.51%</td></tr></table>"
               "<p>The maturity analysis of the Company's financial liabilities:</p>"
               "<table><tr><td></td><td>Less Than 1 Year</td><td>1-3 Years</td><td>5-10 Years</td></tr>"
               "<tr><td>Lease liabilities</td><td>4,211,100</td><td>7,950,000</td><td>11,400,063</td></tr></table>"
               "</body></html>")
    from markut.evidence import core
    monkeypatch.setattr(core, "core_sources", lambda fs: [{"form": "6-K", "name": "rel.htm", "filing_date": "2026-07-16", "url": "u",
                                                           "html": release, "text": "", "headline_period": "quarter"}])
    t = core.extract_core({}, {"segments": [], "company_kpis": []}, None)["text"]
    rev = next((l for l in t.splitlines() if "Net sales" in l), "")
    check(f"TSM-shaped: '2Q26' is current and '2Q25' its year-ago ({rev[:160]!r})",
          "1,270,381 million" in rev and "vs 933,792 million year-ago" in rev)
    check("an equity-statement row is never an income figure", "759,837,230" not in t)
    check("subsidiary ownership percentages are not performance figures", "TSMC Japan" not in t)
    check("valuation assumptions of a compensation note are not performance figures", "Risk-free interest rate" not in t)
    check("a maturity table ('5-10 Years') is not a lease balance", "11,400,063" not in t and "as of 5-10 Years" not in t)
