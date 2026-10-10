# Changes — understand the company before researching it

Runs #10 (AVGO) and #11 (CAKE) showed the pipeline carried one company's
shape: guidance keyed on a heading one filer uses, restaurant metrics
missing, traffic data labeled "customer concentration", one round. This
iteration makes the graph work out what kind of business it is looking at
before research runs, then organizes the debate around that.

## New graph

    profiler -> planner -> research <-> coverage gate -> bull <-> bear -> judge
             -> claim review -> governor

- **profiler** (`markut/agents/profiler.py`, `markut/evidence/sections.py`):
  code extracts 10-K Item 1, the newest MD&A from its overview, the segment
  reporting note (tables kept as rows) and the earnings exhibit by heading;
  one strict-JSON call gives business, archetype, segments, the KPIs the
  company itself reports and accounting flags; a code check retries once and
  otherwise records a coverage gap. Cached in the run store by ticker + the
  accession numbers of the latest 10-K / 10-Q / earnings 8-K.
- **image-only exhibits**: an earnings deck filed as JPEG slides is
  transcribed by the model once per filing (batches of 12 slides, cached by
  accession) and read like any other exhibit. Off with
  `MARKUT_TRANSCRIBE_SLIDES=0`, which records an extraction failure instead.
- **planner**: one strict-JSON call from the profile plus a market summary:
  exactly five questions (each with filing-style search queries, so research
  needs no model call), 1–3 ways standard metrics mislead here, peer tickers
  (stored only).
- **research**: works through Q1–Q5 in order; every excerpt is tagged with
  question id, source, filing date, period, basis and segment; each chunk is
  used once per run and nothing the profiler read is retrieved again;
  "could not find" is written explicitly. General evidence (risk captions,
  executive quotes, revenue and segment figures, every dollar figure in the
  commitments / guarantees / leases / debt notes under the heading actually
  found) and guidance (every statement with forward-looking language and a
  figure, from all 8-K exhibits, transcribed decks and the 10-Q MD&A; an
  EXTRACTION FAILURE line when the language exists but no figure could be
  read) follow. Size limits apply to text excerpts only.
- **coverage gate**: count-only; fewer than 4 of 5 questions with a sourced
  line routes research back once with a wider net on the uncovered ones;
  what is still uncovered becomes a COVERAGE GAP line.
- **packet order**: profile → key questions → evidence under Q1–Q5 → general
  evidence → guidance → valuation and market data → news → what would
  mislead → coverage gaps → data gaps. The packet is the cached prefix of
  every bull / bear / judge / review call.
- **debate**: bull and bear answer Q1–Q5 with `[Qn]` labels, may add one
  `[missed question]`, and must respect the mislead list. The judge rules
  per question (answer, stronger side, confidence, unsupported claims), then
  gives the overall verdict and a `planner_coverage` note. Generic checklist:
  period and basis consistency, overlapping percentages, company
  relationships not stated in the evidence, conclusions drawn from how the
  grid is built, news-only claims.
- Snapshot test of the topology: `tests/fixtures/graph_snapshot.json`.

## Company-specific assumptions removed

| Where | What was there | Now |
|---|---|---|
| `rag.THEMES` sub-queries | "export controls, sanctions…", "foundry manufacturing capacity, wafer supply and packaging", "backstop commitment and residual value guarantee", "we depend on a limited number of customers" | removed; the planner's per-question queries drive retrieval |
| `rag.DETERMINISTIC_BLOCKS` | "Guidance & outlook" keyed on an `Outlook` heading, "Guarantees & commitments — dollar figures" from one 10-Q note, "Customer concentration (dated)" | only "Risk factor titles" and "Business overview" remain; guidance, note dollar lines and dated facts come from general extractors titled by what is found |
| `edgar.extract_outlook` | guidance = the paragraph under a literal "Outlook"/"Guidance" heading | `sections.guidance_lines`: forward-looking language + a figure, any exhibit, slides included |
| `edgar.concentration_sentences` | sentences with "customer" + a percent / "largest" / "backstop" / "lease obligations" (which caught restaurant traffic) | removed |
| `edgar.extract_10q_commitments` | the Commitments and Contingencies note only | `sections.note_dollar_blocks`: commitments, guarantees, leases, debt, borrowings, credit facility, contingencies, each titled by its heading, topic-filtered |
| `edgar._BOILERPLATE_START` | "about broadcom\|about nvidia" | "about \<anything\>" |
| `edgar._OBLIGATION_WORDS` | "backstop" | removed |
| `edgar.extract_highlights` docstring / label strip | "AI revenue lines"; "EX-99.1 Document" only | product-line revenue lines; any `EXHIBIT 99.x` / `Document` prefix in any casing |
| `prompts.JUDGE_CHECKLIST` | "(e.g. distributors 48% + top-five end customers 40%)"; "a product the company itself designs… is not a competitor" | overlapping percentages in general; company relationships only as the evidence states them |
| `prompts.ANALYST_ADDENDUM` | "never call a company's own product, or one of its customers, a competitor" | relationships only where the evidence states them; Q1–Q5 structure |
| `eval/harness.py` | baseline prompt hardcoded "EVIDENCE PACKET for NVDA" | the run's ticker |
| `valuation.EPS_CASES` | fixed −15% / +10% rows | analyst low / consensus / high from the estimate table |
| `validate.py`, console hint, comments | example tickers and company names | neutral examples; comments reworded |

`tests/test_profile_iteration.py::test_prompts_and_code_carry_no_company_assumptions`
scans every prompt and every package file for the list above.

## Bugs from runs #10 and #11

- **Rounds**: `config.MIN_ROUNDS = 2`; the stream, the API and the console all
  floor at two.
- **Governor identifiers**: the digit inside Q4, FY2027, p25, 10-K, 10-Q, 8-K
  and dates never starts a claim; a bare year before a finance noun is a
  label. "Q4 revenue" yields no numeric claim.
- **Governor clause scope**: conjunctions (and, versus, while, compared with…)
  bound a clause, and a period marker joined to the figure by on/of/for/in
  names its basis. "46.37x on trailing TTM GAAP EPS" passes; both stored
  runs' pre-review verdicts produce zero false positives; the real
  mislabel (a quarterly rate called annual) is still caught.
- **Claim review**: claims are deduped (top-level plus per-question); a
  "found verbatim" ruling requires every figure in the claim to be found and
  shows the passage containing the match; the model's entries are matched
  back to the submitted claims (restatements dropped, skipped claims recorded
  as unresolved); every ruling carries one of flag upheld / flag overturned /
  unresolved.
- **FCF** = last four quarters of operating cash flow minus capex from the
  quarterly cash flow statement; the provider's single field is a labeled
  fallback only. The FCF yield uses the same figure.
- **Duplicate turns**: every event carries a `turn_id` (also the SSE `id`
  line); the page paints each id once and a cleared timeline invalidates
  in-flight reveal queues.
- **Scenario grid**: rows are next-FY analyst low / consensus / high; the
  forward P/E line and the grid use the same EPS field (price / forwardEps).

## Measured (2026-10-08, Sonnet 5.5; baseline = AVGO run #10, 98.7K input tokens)

| run | ticker | state | input tokens | × baseline | calls | coverage | governor flags | notes |
|---|---|---|---|---|---|---|---|---|
| #12 | CAKE | cold, 37-slide deck transcribed | 227.8K | 2.31× | 14 | 5/5 | 0 | first cut; packet 45K chars (trimmed afterwards) |
| #14 | AVGO | cold | 172.2K | 1.74× | 11 | 5/5 | 0 | planner retry (output cap, fixed) |
| #15 | CAKE | profile cached, plan fresh | 154.1K | 1.56× | 9 | 5/5 | 0 | all done-when needles present |
| #16 | AVGO | profile + plan cached | 146.8K | 1.49× | 8 | 5/5 | 0 | all done-when needles present, 14/15 cited |
| #17 | JPM | held out, cold | 143.6K | 1.46× | 10 | 5/5 | 0 | archetype bank; see below |

Done-when checks: CAKE packet carries the FY2026 consolidated-sales and
net-income-margin assumptions (from the transcribed deck), the restaurant-
level margin, North Italia and Flower Child figures and traffic vs check;
archetype consumer_retail_restaurant; two rounds. AVGO packet carries AI
semiconductor revenue and growth, the Q4 revenue guide, the ~$29B maximum
liability and the up-to-$42B convertible notes, FCF $39.4B; archetype
semis_hardware. Zero governor false positives and unique turn ids on all.

Where the tokens go on a warm run (AVGO #16): the ~15.5K-token cached
packet is written once per distinct output schema (bull/bear, judge, claim
review, governor: 4 writes ≈ 62K) and read 4 more times (≈ 61K); the
uncached remainder is ≈ 24K. Cold runs add the profiler (≈ 12K, or ≈ 50K
once per filing when a slide deck has to be transcribed) and the planner
(≈ 4K).

### Held-out JPM (not tuned)

- Archetype `bank`; segments CCB / CIB / AWM; KPIs the bank itself reports
  (NII ex-Markets, ROTCE, TBVPS, CET1, card net charge-off rate, AUM).
- Guidance: the first pass found none, because the outlook sits in the 10-Q
  under an executive overview ahead of the "Item 2" MD&A heading and the scan
  only read the extracted MD&A. General fix applied: the scan reads the whole
  10-Q narrative. It now returns the NII, adjusted-expense and card
  charge-off outlooks and the dividend increase. The earnings supplement
  (exhibit 99.2) still reports an EXTRACTION FAILURE: it has forward-looking
  language and no guided figure in text.
- Section labels fit: note blocks came out as "Guarantees (10-Q note)" and
  "Debt (10-Q note)"; no restaurant- or semiconductor-shaped label appeared.
- Segment tags on JPM lines were all "unspecified" because the profile names
  segments as "Consumer & Community Banking (CCB)" while the text says CCB.
  General fix applied: abbreviations and the name without its parenthetical
  are aliases.
- A past-tense provision sentence passed as guidance because "assumptions"
  counted as forward-looking. General fix applied: a past-tense statement
  without a forward verb is never guidance.
- Figures the planner states in its questions are now checked against the
  profile and market summary it was shown (rounding tolerated); the profile's
  figures against the filing sections. Unverifiable figures are replaced by a
  visible marker and counted as a profile gap.


# Changes — reliable numbers and labels (2026-10-09)

Runs #19 and #20 showed the profiler and planner generalizing (right business
type, KPIs and questions) while the data underneath failed: KPI values and
worded guidance present in filings were not extracted, table rows lost their
section and period labels, six-month figures were tagged annual, the coverage
gate counted filler, extraction failures were argued as "doesn't exist", and
a planner note stated a wrong fact about how a data field is computed.

## What changed

1. **Table parser with context** (`edgar.parse_tables`, `period_from_heading`).
   Every row carries the table caption, its units ("in thousands"), the group
   heading it sits under (a single-cell row in the label column, or a label
   cell shared with the header row), and one period heading per value column
   built from stacked header rows by column position with colspans. Date-only
   headings take their duration from the table or the release headline and
   otherwise read "period ended <date>"; never "unstated" under headings.
2. **Core extraction first** (`evidence/core.py`, new `[CORE FIGURES]` block
   after `[KEY QUESTIONS]`). Before any question: income summary (revenue,
   gross profit, operating income, net income, EPS, adjusted variants), the
   company's KPI table, segment / revenue-line rows (one per segment column or
   per segment-named row), cash flow summary. Each figure is one line with
   current and year-ago values (paired within the same column entity and
   period class), the change computed in code, period, basis, units, segment
   and source. Every profiled and planner-linked KPI gets a value line or
   "KPI value not found". Derived metrics (gross/operating/net margin from the
   same table and period; the residual margin after cost ratios printed as %
   of revenue) and sanity checks (segment columns vs the Total column; stated
   growth vs the two values) show their formula and inputs. Core lines are
   tagged `[Qn]` where their label or segment matches a question's KPIs,
   segments or key terms. The old "revenue and segment figures" block is
   gone; prose revenue statements from the release stay in general evidence.
3. **Guidance in words and ranges** (`sections.interpret_band`). A lead-in
   ending with ":" travels into each of its bullets, so "we expect: • Revenue
   to grow at a low-thirties percentage rate" keeps its verb. Worded ranges
   become interpreted bands beside the original wording: low-thirties →
   30–33%, mid-to-high twenties → 24–29%, high-single-digit → 7–9%, "33% to
   34%" → 33–34%, flat → ≈0%, "similar to the second quarter" → the prior
   period's value. EXTRACTION FAILURE only when nothing parses.
4. **Period classes** YTD (H1) and YTD (9M) in the tracer and every tag; a
   partial-year marker outranks the fiscal year it sits in ("first six months
   of fiscal 2026" is H1, not annual); "three fiscal quarters" is 9M; the
   classes conflict with quarter, TTM and annual in the label check.
5. **Unknown is not absent.** Bull, bear and judge prompts say an EXTRACTION
   FAILURE / not-found line means "not retrieved", judge checklist item 6; the
   governor finds sentences asserting non-disclosure while the packet records
   a failure, hands them to the revision and stamps survivors
   `[UNKNOWN, NOT ABSENT — …]`.
6. **Coverage gate counts real answers.** A question is covered only when a
   candidate line (question-tagged excerpt or tagged core figure, boilerplate
   and risk captions excluded) holds a figure or a direct factual statement
   AND every KPI the planner linked to it has a value or an explicit
   not-found; one cheap model call (`COVERAGE_SCHEMA`, ~800 output tokens)
   rates relevance across all five questions; the routing stays in
   `graph.coverage_decision`.
7. **Leverage from the balance sheet** (`market.leverage_lines`): debt/equity
   including and excluding lease obligations from the latest quarterly
   balance sheet, each component in dollars, the one matching the provider's
   figure labeled. The provider line now says it is a data-provider field.
   Planner "what would mislead" items that describe how a field is computed
   are rewritten by code to "Check whether …" unless they cite a packet line;
   the planner prompt says so.
8. **Derived KPIs in code** — see 2. Models never do this arithmetic.
9. **Profile trimming** cuts KPI definitions, then the business text; never
   accounting flags, segment figures or KPI names.
10. **Governor normalization**: a value whose label carries % ("Debt/Equity
    (mrq, %): 401.00") also anchors "401%"/"401.00%"; a value on a line that
    states its units ("$729,476 thousand", "(units: millions)") also anchors
    its scaled form ("$729.476M", "$3.583B").
11. **Third-party models**: a DCF more than 50% from price is labeled
    "unverified third-party model, assumptions not disclosed; excluded from
    the verdict"; judge checklist item 7 gives it no weight.
12. **Housekeeping**: pages reference `/static/debate.js?v=<hash>` and are
    served with `Cache-Control: no-cache`, static files revalidate on every
    load (a browser can no longer keep the previous page script, which is
    the only duplicate-render path left: the stored runs hold one event per
    turn); the PDF renders the judge's per-question rulings and planner
    coverage and the claim-review verdict labels.

Fixtures: `tests/fixtures/cake_release_tables.html`,
`tests/fixtures/shop_release_tables.html` (real release tables: stacked
period headers, brand group headings, units captions, KPI table).
Tests: `tests/test_reliable_numbers.py`.

## Measured (2026-10-10; baseline = AVGO run #10, 98.7K input tokens)

| run | ticker | state | input tokens | × baseline | coverage (relevant/candidates) | governor flags |
|---|---|---|---|---|---|---|
| #21 | SHOP | profile + plan cached | 160.4K | 1.63× | 4/5 covered (Q2 held back: a linked KPI had no value) | 0 |
| #22 | CAKE | profile + plan cached | 238.3K | 2.41× | 4/5 | 0 |
| #23 | AVGO | profile + plan cached | 184.8K | 1.87× | 4/5 | 0 |
| #24 | XOM | held out, cold | 172.2K | 1.74× | 4/5 | 0 |

Done-when checks: SHOP's core block carries GMV 115,567 vs 87,837, MRR 221 vs
185, gross profit 1,708 vs 1,302, operating income 488 vs 291 (all quarter,
US$ millions, with the change computed in code) and all five Q3 guided items
as interpreted bands (low-thirties → 30–33%, mid-to-high twenties → 24–29%,
33% to 34%, $150 million, high-teens to low-twenties → 17–19% / 20–23%).
CAKE's brand rows are labeled by brand, its six-month lines are tagged
YTD (H1) with zero tagged annual, debt/equity is shown including (401.0%,
matching the provider) and excluding (108.5%) lease obligations, and the
residual margin after cost ratios is a DERIVED line. AVGO's packet carries
the AI semiconductor revenue line, the Q4 guide, the ~$29B maximum liability
and the up-to-$42B convertible notes. Zero governor false positives and unique
turn ids on all four.

CAKE #22 ran at 2.41× because the first core block was verbose (69 lines,
20.8K characters, a repeated source caption on every line) and question
words over-tagged core lines (Q3 had 41 candidates). Both fixed after the
run: a source-table legend with short tags, compact attribute triples,
tighter caps, and tagging by KPI / segment aliases only (two question words
needed otherwise). The CAKE rerun after the fix (#25) came in at 208.0K input (2.11×) with 5/5 questions covered and zero governor flags.

### Held-out XOM (not tuned)

- Archetype `energy_commodity`; segments Upstream and Energy Products (the
  Chemical and Specialty Products segments were not read because the 10-K
  Item 1 and the segment note were not located; see below); seven KPIs the
  company itself reports (earnings excluding identified items, structural
  cost savings, cash capex, advantaged volume growth, production, cash flow
  excluding margin postings, debt-to-capital).
- Core extraction found guidance (capex $27–29B for 2026, $20B of
  repurchases in 2026, structural cost savings reaching $20B by 2030) and the
  segment table of the 10-Q, but only one of seven KPI values. General
  patterns: (1) the release's KPI tables have bullet or missing captions and
  labels that differ from the KPI names the company uses in prose ("Earnings
  excluding Identified Items" in text vs a table row "Earnings excl.
  Identified Items"); (2) the income summary first picked the
  "attributable to noncontrolling interests" row because its label matched
  the net-income pattern before the company's own row — fixed generally
  (rows mentioning noncontrolling interests are skipped, "total revenues and
  other income" counts as revenue); (3) a volume row printed "(kbd)" got the
  table's dollar units appended — fixed generally (a parenthetical unit in
  the label wins); (4) a bullet character and a stray word were taken as
  table captions — fixed generally (a caption needs two real words).
- Profiler sections: the 10-K Item 1 and the segment note were not located by
  the line-anchored heading patterns; the profile was built from the 10-Q MD&A
  and the release. Pattern: filers whose item headings are not on their own
  line or whose segment note is titled differently. Proposed fix: fall back to
  the first paragraph that starts with "Item 1" followed by business language,
  and accept "segment" anywhere in a note heading; not applied here.
- Labels fit the company (quarter, YTD (H1), "Guarantees (10-K note)"); no
  restaurant- or semiconductor-shaped label appeared. The planner's mislead
  items were already phrased as checks.
