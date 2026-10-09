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
