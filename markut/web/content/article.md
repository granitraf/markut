# Five agents, one ticker: what a bull-vs-bear debate swarm gets right (and wrong)

*Draft — written by Granit Rrafshi. Edit this file (`markut/web/content/article.md`) and the public page updates on reload. Plain markdown: headings, lists, bold, links, tables, code blocks.*

## Why a debate instead of one answer

Ask a single language model for an investment view and it will give you one, fluently and with numbers. The trouble is that fluency and grounding are unrelated. Markut was built on a different bet: that making two models argue opposite sides over the *same* evidence, and making a third rule on them, produces a view whose numbers you can trace.

The result is a swarm of five roles on a LangGraph state machine:

1. **Research** assembles one immutable evidence packet per debate: live market data, the latest SEC filings retrieved by a local RAG index, and recent news with its trust level labelled. It discovers the three sources as MCP tools; it never knows their implementation.
2. **Bull** and **Bear** argue in rounds. From round two each one rebuts the other directly.
3. **Judge** reads the whole transcript plus the packet and returns strict JSON: strongest point on each side, claims it could not support, and whether the debate has converged.
4. **Claim review** re-audits the judge's unsupported claims against the packet only — news leads are shown but never count as evidence.
5. **Governed verdict** is a deterministic layer, not a model. It extracts every number in the verdict, traces it to the packet, allows one bounded model revision, and tags whatever still has no anchor as `[UNGROUNDED]` instead of deleting it.

## What the governor measures

Every run ends with a stats line: how many numeric claims the verdict made, how many trace to the evidence, how many the revision derived from cited anchors or labelled as judgment, and how many ended up tagged. On the NVDA course run the debate verdict traced 94% of its numbers; a single-call baseline prompt given the identical packet traced 84%. The baseline was broader on risk recall, 5 of 7 against 4 of 7, and roughly ten times cheaper. Those are the honest trade-offs: the debate grounds better; the single call covers more ground.

| | Debate swarm | Single call (same packet) |
|---|---|---|
| Numbers traced to evidence | 94% | 84% |
| Risk factors recalled | 4 / 7 | 5 / 7 |
| Relative token cost | ~9.8× | 1× |

## What I learned building it

- **Grounding is a property of the pipeline, not the prompt.** The biggest grounding gains came from the deterministic tracer and from giving the judge the packet, not from prompt wording.
- **Fail closed.** When the judge's JSON is unparseable twice, the graph terminates with converged=true rather than looping. When the revision is invalid, the original verdict is kept and tagged. A crashed run costs less than an unbounded one.
- **Budgets are guardrails.** A soft token budget closes the debate through the normal governed exit; a hard stop at twice the budget refuses to start another call.
- **News is dangerous evidence.** Most of the judge's unsupported-claim flags in live runs are numbers that came from news bodies, not filings. Labelling trust level per item and keeping leads out of resolution was the right call.

## Open questions

Three rounds versus two. Whether a fourth debater (a "devil's advocate" on the evidence quality itself) helps. Pairwise LLM judging across tickers rather than an n=1 case study. These are listed as future work in the course notebook and remain open.

*This is a research project, not investment advice. Nothing on this site recommends buying or selling anything.*
