/* Markut shared front-end: mini-markdown, argument parsing, the debate
   timeline (one card per agent event, read-along reveal on replays) and the
   SSE player. No dependencies, no CDN — the pages work offline. Exposes
   window.Markut. */
window.Markut = (function () {
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const num = (n) => (n == null ? "–" : Number(n).toLocaleString());
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // ---- numbers are the only emphasis: $4.74T, 15.32x, 62.97%, 1,234.5, FY2026, Q2 ----
  const NUM_RE = /(\$[\d,]+(?:\.\d+)?\s?(?:[BMTK]|bn|billion|million|trillion)?|\d+(?:\.\d+)?\s?%|\d+(?:\.\d+)?x\b|\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b|\b\d+\.\d+\b|\b(?:FY|Q[1-4]\s?FY?)\d{2,4}\b)/g;
  function highlightNums(html) {
    // apply only to text outside tags so an existing <span> is never re-wrapped
    return html.split(/(<[^>]+>)/).map((part) => part.startsWith("<") ? part : part.replace(NUM_RE, '<span class="n">$1</span>')).join("");
  }

  // ---- mini-markdown: headings, lists (-, *, 1.), bold/italic/code, links, hr, quotes, fenced code, tables ----
  // opts.plainBold: the model bolds half its nouns — in argument bodies bold is dropped and numbers carry the emphasis
  function inline(s, opts = {}) {
    let out = esc(s)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*(.+?)\*\*/g, opts.plainBold ? "$1" : "<strong>$1</strong>")
      .replace(/(^|\W)\*(?!\s)(.+?)\*(?=\W|$)/g, "$1<em>$2</em>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+|\/[^\s)]*|#[^\s)]*)\)/g, '<a href="$2" rel="noopener">$1</a>');
    return opts.nums ? highlightNums(out) : out;
  }
  function md(text, opts = {}) {
    const out = []; let list = null, listTag = "ul", para = [], code = null, table = null;
    const flushP = () => { if (para.length) { out.push("<p>" + inline(para.join(" "), opts) + "</p>"); para = []; } };
    const flushL = () => { if (list) { out.push(`<${listTag}>` + list.join("") + `</${listTag}>`); list = null; } };
    const flushT = () => { if (table) { out.push("<table>" + table.join("") + "</table>"); table = null; } };
    for (const raw of String(text || "").split("\n")) {
      const line = raw.replace(/\s+$/, "");
      let m;
      if (code !== null) { if (/^```/.test(line)) { out.push("<pre><code>" + esc(code.join("\n")) + "</code></pre>"); code = null; } else code.push(raw); continue; }
      if (/^```/.test(line)) { flushP(); flushL(); flushT(); code = []; continue; }
      if (/^\|.*\|\s*$/.test(line)) {
        flushP(); flushL();
        const cells = line.slice(1, -1).split("|").map((c) => c.trim());
        if (cells.every((c) => /^:?-{2,}:?$/.test(c))) continue;
        const tag = table ? "td" : "th";
        (table = table || []).push("<tr>" + cells.map((c) => `<${tag}>${inline(c, opts)}</${tag}>`).join("") + "</tr>");
        continue;
      }
      flushT();
      if (!line.trim()) { flushP(); flushL(); continue; }
      if (/^(-{3,}|\*{3,})$/.test(line.trim())) { flushP(); flushL(); if (!opts.noRules) out.push("<hr>"); continue; }
      if ((m = line.match(/^(#{1,6})\s+(.*)$/))) { flushP(); flushL(); const h = Math.min(m[1].length, 4); out.push(`<h${h}>${inline(m[2], opts)}</h${h}>`); continue; }
      if ((m = line.match(/^\s*[-*•]\s+(.*)$/))) { flushP(); if (list && listTag !== "ul") flushL(); listTag = "ul"; (list = list || []).push("<li>" + inline(m[1], opts) + "</li>"); continue; }
      if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) { flushP(); if (list && listTag !== "ol") flushL(); listTag = "ol"; (list = list || []).push("<li>" + inline(m[1], opts) + "</li>"); continue; }
      if ((m = line.match(/^>\s?(.*)$/))) { flushP(); flushL(); out.push("<blockquote>" + inline(m[1], opts) + "</blockquote>"); continue; }
      flushL(); para.push(line);
    }
    if (code !== null) out.push("<pre><code>" + esc(code.join("\n")) + "</code></pre>");
    flushP(); flushL(); flushT();
    return out.join("");
  }

  /* splitArgument: a bull/bear case arrives as "# Title", then numbered points
     ("## 1. ...", or a bold line "**1. ...**"), each with its paragraph(s).
     Headings become the one-line summary; bodies collapse beneath them. */
  function splitArgument(text) {
    const lines = String(text || "").split("\n");
    let title = "", intro = [], sections = [], cur = null;
    const isHead = (l) => l.match(/^#{1,6}\s+(.*)$/) || l.match(/^\*\*(\d+[.)]\s[^*]{3,})\*\*\s*$/);
    for (const raw of lines) {
      const line = raw.replace(/\s+$/, "");
      if (/^(-{3,}|\*{3,})$/.test(line.trim())) continue;           // the model's rules are noise
      const t = line.match(/^#\s+(.*)$/);
      if (t && !title && !sections.length) { title = t[1].replace(/\*\*/g, ""); continue; }
      const h = isHead(line);
      if (h) { cur = { heading: h[1].replace(/\*\*/g, "").trim(), body: [] }; sections.push(cur); continue; }
      (cur ? cur.body : intro).push(line);
    }
    return { title, intro: intro.join("\n").trim(), sections: sections.map((s) => ({ heading: s.heading, body: s.body.join("\n").trim() })) };
  }
  const ARG = { plainBold: true, nums: true, noRules: true };
  function argumentHtml(text) {
    const { title, intro, sections } = splitArgument(text);
    if (sections.length < 2) {
      // unstructured reply: first paragraph visible, the rest behind "read more"
      const paras = String(text || "").split(/\n{2,}/);
      const head = md(paras[0], ARG), rest = paras.slice(1).join("\n\n");
      return `<div class="arg md">${title ? `<h2 class="argtitle">${inline(title, ARG)}</h2>` : ""}${head}${rest ? `<details class="pt"><summary>read the rest</summary><div class="md">${md(rest, ARG)}</div></details>` : ""}</div>`;
    }
    return `<div class="arg">${title ? `<h2 class="argtitle">${inline(title, ARG)}</h2>` : ""}` +
      (intro ? `<div class="md intro">${md(intro, ARG)}</div>` : "") +
      `<div class="pts">` + sections.map((s, i) => `<details class="pt" data-i="${i}"><summary><span class="k">${i + 1}</span><span class="h">${inline(s.heading.replace(/^\d+[.)]\s*/, ""), ARG)}</span></summary><div class="md">${md(s.body, ARG)}</div></details>`).join("") + `</div>` +
      `<div class="argtools"><a href="#" class="expand">expand all</a></div></div>`;
  }

  // the governor's inline tag: "<number> [UNGROUNDED — no evidence anchor]" -> highlighted span; other numbers get the quiet highlight
  function verdictHtml(text) {
    let html = esc(text || "");
    html = html.replace(/(\S+)\s\[UNGROUNDED — no evidence anchor\]/g, '<span class="ungrounded">$1 <small>UNGROUNDED · no evidence anchor</small></span>');
    html = highlightNums(html);
    return html.split(/\n{2,}/).map((p) => "<p>" + p.replace(/\n/g, "<br>") + "</p>").join("");
  }
  function taggedNumbers(text) { return [...String(text || "").matchAll(/(\S+)\s\[UNGROUNDED — no evidence anchor\]/g)].map((m) => m[1]); }

  /* packetHtml: the market section of the evidence packet is "[SECTION]" headers
     and "- Key: value  [source: x]" lines — shown as a grid; everything after
     (filings, news) stays collapsed as text. */
  /* packetHtml: the evidence packet's market lines ("[SECTION]" headers and
     "- Key: value  [source: x]") become a two-column key/value grid; the
     valuation block's EPS×multiple scenario rows become a small table;
     BASIS/PERIOD notes become full-width text. Values never wrap —
     labels do. Everything after the market lines (filings, news) stays
     collapsed as text. */
  const WRAP_SECTIONS = /^(PROFILE|KEY QUESTIONS|WHAT WOULD MISLEAD|COVERAGE GAPS)$/;
  function packetHtml(evidence) {
    const lines = String(evidence || "").split("\n");
    const sections = []; let s = null;
    const push = (name, intro) => { s = { name, intro: intro || "", rows: [], notes: [], scenarios: [], text: [] }; sections.push(s); };
    for (const raw of lines) {
      const l = raw.trim(); let m;
      if (!l) { if (s && s.text.length && s.text[s.text.length - 1] !== "") s.text.push(""); continue; }
      if ((m = l.match(/^\[([A-Z][A-Z0-9 &\/]+)\](?:\s+\((.*)\))?\s*$/)) && !/unavailable/.test(l)) { push(m[1], m[2]); continue; }
      if ((m = l.match(/^\[(EVIDENCE Q\d|FILINGS|NEWS|GUIDANCE|GENERAL EVIDENCE)\]\s*(.*)$/))) { push(m[1], m[2]); continue; }
      if (!s) push("EVIDENCE");
      const body = l.replace(/\s*\[source:[^\]]*\]\s*$/, "");
      const quoteOrTag = /^-\s+(\[|")/.test(body);
      if (!quoteOrTag && (m = body.match(/^-\s+((?:BASIS|PERIOD)\s+NOTE|[A-Z][A-Z ]{2,}NOTE)\s*:\s*(.*)$/))) { s.notes.push(m[2]); continue; }
      if (!quoteOrTag && (m = body.match(/^-\s+Scenario grid\s*\((.*)\)\s*$/))) { s.notes.push("Scenario grid: " + m[1]); continue; }
      if (!quoteOrTag && (m = body.match(/^-\s+Implied price, EPS\s+(.*?)\s+(\$[\d.]+):\s+(.*)$/))) {   // scenario grid row: "name mult = $price (pct)" cells
        const cells = m[3].split("|").map((c) => { const cm = c.trim().match(/^(.*?)\s+([\d.]+x)\s+=\s+(\$[\d,.]+)\s+\(([-+][\d.]+%)\)/); return cm ? { name: cm[1], mult: cm[2], price: cm[3], pct: cm[4] } : null; }).filter(Boolean);
        s.scenarios.push({ label: "EPS " + m[1], eps: m[2], cells }); continue;
      }
      if (s.name === "DATA GAPS" && (m = body.match(/^-\s+(.*)$/)) && !/:\s/.test(m[1])) { s.rows.push({ k: m[1], v: "" }); continue; }
      if (!quoteOrTag && (m = body.match(/^-\s+([^:]{3,120}):\s*$/))) { s.rows.push({ sub: m[1] }); continue; }         // bare sub-header
      if (!quoteOrTag && (m = body.match(/^-\s+([^:"]{2,120}?):\s+(.+)$/))) { s.rows.push({ k: m[1], v: m[2] }); continue; }
      s.text.push(l);                                                 // evidence quotes, tagged lines, block headings
    }
    const sectionHtml = (sec) => {
      if (sec.name === "DATA GAPS") {
        const items = sec.rows.map((r) => r.sub ? null : { k: r.k, v: r.v }).filter(Boolean);
        const missing = items.filter((r) => !/^known gap/.test(r.k) && !/^none/.test(r.k));
        return `<div class="gaps ${missing.length ? "has" : ""}"><span class="gk">data gaps</span>` +
          (missing.length ? missing.map((r) => `<span class="gi"><b>${esc(r.k)}</b> ${esc(r.v)}</span>`).join("") : `<span class="gi">none — every source responded</span>`) +
          items.filter((r) => /^known gap/.test(r.k)).map((r) => `<span class="gi known">${esc(r.v)}</span>`).join("") + `</div>`;
      }
      const wrap = WRAP_SECTIONS.test(sec.name);
      const kv = sec.rows.filter((r) => !r.sub);
      const isText = !kv.length && !sec.scenarios.length && sec.text.length;
      if (isText) {
        const body = sec.text.join("\n").trim();
        const n = sec.text.filter((t) => /^- /.test(t)).length;
        const label = sec.name.toLowerCase() + (sec.intro ? " · " + sec.intro : "") + (n ? ` · ${n} line${n === 1 ? "" : "s"}` : "");
        return `<details class="pt pk-text"><summary>${esc(label)}</summary><pre class="evidence">${esc(body)}</pre></details>`;
      }
      let h = `<div class="pk ${wrap ? "wrap" : ""}"><div class="pkh">${esc(sec.name.toLowerCase())}</div>`;
      if (kv.length) h += `<div class="pkg">` + kv.map((r) => `<div class="pkr"><span class="pkk">${esc(r.k)}</span><span class="pkv">${esc(r.v)}</span></div>`).join("") + `</div>`;
      if (sec.scenarios.length) {
        const cols = sec.scenarios[0].cells.map((c) => c.name);           // p25 / median / p75, or 0.8x fwd / fwd / 1.2x fwd
        h += `<table class="pkt"><thead><tr><th>implied price (EPS × multiple)</th>${cols.map((c, i) => `<th>${esc(c)} <small>${esc(sec.scenarios[0].cells[i].mult)}</small></th>`).join("")}</tr></thead><tbody>` +
          sec.scenarios.map((r) => `<tr><td>${esc(r.label)} <b>${esc(r.eps)}</b></td>` + cols.map((n) => { const c = r.cells.find((x) => x.name === n); return c ? `<td><b>${esc(c.price)}</b> <small>${esc(c.pct)}</small></td>` : "<td>–</td>"; }).join("") + `</tr>`).join("") + `</tbody></table>`;
      }
      h += sec.notes.map((n) => `<div class="pkn">${esc(n)}</div>`).join("");
      if (sec.text.length) h += `<pre class="evidence small">${esc(sec.text.join("\n").trim())}</pre>`;
      return h + `</div>`;
    };
    const used = sections.filter((x) => x.rows.length || x.notes.length || x.scenarios.length || x.text.length);
    if (!used.length) return `<pre class="evidence">${esc(evidence || "")}</pre>`;
    // data gaps first (a reader checks what is missing before reading), then the packet in its own order
    const gaps = used.filter((x) => x.name === "DATA GAPS"), others = used.filter((x) => x.name !== "DATA GAPS");
    return gaps.map(sectionHtml).join("") + others.map(sectionHtml).join("");
  }

  const kvs = (pairs) => '<div class="kv">' + pairs.map(([k, v]) => `<div><div class="k">${k}</div><div class="v">${v}</div></div>`).join("") + "</div>";
  const note = (d) => (d && d.note ? `<p class="note">${esc(d.note)}</p>` : "");

  /* Timeline(el, hooks): renders events into el.
     hooks: onStart(d), onEnd(kind, d), onSaved(d), autoscroll (bool), reveal (bool: read-along pacing), charMs
     With reveal on, renders are queued and each card's summary lines type out at reading pace before the next card. */
  function Timeline(el, hooks = {}) {
    let pending = null, maxRounds = 0, queue = Promise.resolve(), reveal = !!hooks.reveal, charMs = hooks.charMs || 9;
    const ctx = { round: 0 };
    // every event carries a turn_id unique within its run: a turn seen twice
    // (a replayed queue, a reconnect, a double-wired listener) paints once
    let seen = new Set(), generation = 0;
    function card(cls, html) {
      const c = document.createElement("div"); c.className = "card " + cls; c.innerHTML = html;
      el.appendChild(c); wire(c);
      if (c.scrollIntoView && hooks.autoscroll !== false) c.scrollIntoView({ block: "nearest", behavior: "smooth" }); return c;
    }
    function wire(c) {
      const ex = c.querySelector(".argtools .expand");
      if (ex) ex.addEventListener("click", (e) => { e.preventDefault(); const open = ex.textContent === "expand all"; c.querySelectorAll("details.pt").forEach((d) => { d.open = open; }); ex.textContent = open ? "collapse all" : "expand all"; });
    }
    function clearPending() { if (pending) { pending.remove(); pending = null; } }
    function setPending(label) { clearPending(); const where = maxRounds && ctx.round ? `round ${ctx.round}/${maxRounds} · ` : ""; pending = card("pending", `<span class="dot"></span>${esc(where + label)}`); }
    const roundTag = (d) => (d.round ? `<span class="round">round ${d.round}${maxRounds ? "/" + maxRounds : ""}</span>` : "");

    // ---- read-along reveal: type the summary lines (title + point headings), fade the rest ----
    async function typeInto(node, text) {
      node.textContent = ""; node.classList.add("typing");
      for (const ch of text) { node.textContent += ch; await sleep(charMs); }
      node.classList.remove("typing");
    }
    async function revealCard(c) {
      if (!reveal) return;
      const targets = [...c.querySelectorAll(".argtitle, summary .h, .sc .v, h3 .who")];
      c.classList.add("revealing");
      for (const t of targets) { const full = t.innerHTML; const plain = t.textContent; t.dataset.html = full; await typeInto(t, plain); t.innerHTML = full; }
      c.classList.remove("revealing"); c.classList.add("revealed");
      await sleep(350);
    }
    const enqueue = (fn) => { const g = generation; queue = queue.then(() => (g === generation ? fn() : undefined)).catch(() => {}); return queue; };

    const pill = (t, cls) => `<span class="pill ${cls || ""}">${esc(t)}</span>`;
    const paint = {
      start(d) { maxRounds = d.max_rounds || 0; ctx.round = 0; if (hooks.onStart) hooks.onStart(d); if (d.note) card("route", esc(d.note)); setPending("profiler is reading the latest 10-K, 10-Q and earnings release…"); },
      profiler(d) {
        clearPending();
        const p = d.profile || {};
        let body = p.business ? `<p class="md">${esc(p.business)}</p>` : `<p class="note">no profile could be built${(d.gaps || []).length ? ": " + esc(d.gaps.join("; ")) : ""}</p>`;
        const segs = (p.segments || []).filter((x) => x && x.name);
        if (segs.length) body += `<table class="pkt"><thead><tr><th>segment</th><th>latest revenue</th><th>yoy</th><th>period</th></tr></thead><tbody>` +
          segs.map((x) => `<tr><td>${esc(x.name)}</td><td><b>${esc(x.latest_revenue || "—")}</b></td><td>${esc(x.latest_yoy || "—")}</td><td><small>${esc(x.period || "")}</small></td></tr>`).join("") + `</tbody></table>`;
        const kpis = (p.company_kpis || []).filter((x) => x && x.name);
        if (kpis.length) body += `<div class="kpis"><span class="k">the metrics this company reports</span><ul class="claims">${kpis.map((x) => `<li><b>${esc(x.name)}</b>${x.unit ? ` <small>(${esc(x.unit)})</small>` : ""} — ${esc(x.definition || "")}${x.segment && !/company-?wide/i.test(x.segment) ? ` <small>· ${esc(x.segment)}</small>` : ""}</li>`).join("")}</ul></div>`;
        if ((d.gaps || []).length) body += `<p class="note">profile gaps: ${esc(d.gaps.join("; "))}</p>`;
        const c = card("profile", `<h3><span class="who">profiler</span> ${p.archetype ? pill(String(p.archetype).replace(/_/g, " "), "yes") : ""}${(p.accounting_flags || []).map((f) => pill(String(f).replace(/_/g, " "))).join(" ")}${d.cached ? pill("from cache", "live") : ""}</h3>${body}`);
        setPending("planner is choosing the five questions that decide the outlook…"); return c;
      },
      planner(d) {
        clearPending();
        const plan = d.plan || {};
        let body = `<ol class="qs">${(plan.key_questions || []).map((q) => `<li><b>${esc(q.id || "")}</b> ${esc(q.question || "")}${(q.kpis || []).length ? ` <small>· ${esc(q.kpis.slice(0, 4).join(", "))}</small>` : ""}</li>`).join("")}</ol>`;
        if ((plan.what_would_mislead || []).length) body += `<div class="mislead"><span class="k">what would mislead here</span><ul class="claims">${plan.what_would_mislead.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>`;
        const c = card("plan", `<h3><span class="who">planner</span> key questions${d.cached ? " " + pill("from cache", "live") : ""}</h3>${body}`);
        setPending("research agent is working through Q1–Q5 in the filings, market data and news…"); return c;
      },
      research(d) {
        clearPending();
        const cov = d.coverage || {}; const ids = Object.keys(cov);
        const chips = ids.length ? `<div class="cov">${ids.map((q) => `<span class="pill ${cov[q] ? "yes" : "no"}">${esc(q)} · ${cov[q]} line${cov[q] === 1 ? "" : "s"}</span>`).join(" ")}${d.pass > 1 ? ` <small>pass ${d.pass}</small>` : ""}</div>` : "";
        const c = card("research", `<h3><span class="who">research</span> evidence packet${d.pass > 1 ? ` <span class="round">pass ${d.pass}</span>` : ""}</h3>${chips}${packetHtml(d.evidence)}`);
        ctx.round = 1; setPending("bull analyst is building the upside case…"); return c;   // a coverage event (new runs) replaces this
      },
      coverage(d) {
        const again = d.decision === "research";
        card("route", `${again ? "↻" : "→"} coverage gate: ${d.covered}/${d.total} questions have sourced evidence${again ? ` — second pass on ${esc((d.uncovered || []).join(", "))}` : (d.uncovered && d.uncovered.length ? ` — ${esc(d.uncovered.join(", "))} left as coverage gaps` : "")}`);
        setPending(again ? `research agent is taking a wider pass on ${esc((d.uncovered || []).join(", "))}…` : "bull analyst is building the upside case…");
      },
      bull(d) { clearPending(); ctx.round = d.round || ctx.round; const c = card("bull", `<h3><span class="who">bull</span> ${roundTag(d)}</h3>${argumentHtml(d.text)}`); setPending("bear analyst is building the downside case…"); return c; },
      bear(d) { clearPending(); ctx.round = d.round || ctx.round; const c = card("bear", `<h3><span class="who">bear</span> ${roundTag(d)}</h3>${argumentHtml(d.text)}`); setPending("judge is weighing both sides against the evidence…"); return c; },
      judge(d) {
        clearPending(); ctx.round = d.round || ctx.round;
        let body = "";
        if (d.recorded === false) body = note(d);
        else {
          body += `<div class="sc"><div class="scb"><div class="k">strongest bull point</div><div class="v">${highlightNums(esc(d.bull_strongest || "—"))}</div></div><div class="scr"><div class="k">strongest bear point</div><div class="v">${highlightNums(esc(d.bear_strongest || "—"))}</div></div></div>`;
          const qs = (d.questions || []).filter((q) => q && q.id);
          if (qs.length) body += `<table class="pkt qt"><thead><tr><th>question</th><th>what the evidence supports</th><th>stronger</th><th>conf.</th></tr></thead><tbody>` +
            qs.map((q) => `<tr><td><b>${esc(q.id)}</b></td><td>${highlightNums(esc(q.answer || ""))}${(q.unsupported_claims || []).length ? `<div class="qflag">unsupported: ${esc(q.unsupported_claims.join(" · "))}</div>` : ""}</td><td><span class="side ${esc(q.stronger_side || "")}">${esc(q.stronger_side || "")}</span></td><td><small>${esc(q.confidence || "")}</small></td></tr>`).join("") + `</tbody></table>` +
            (d.planner_coverage && !/^adequate\.?$/i.test(d.planner_coverage) ? `<p class="note">planner coverage: ${esc(d.planner_coverage)}</p>` : "");
          const claims = d.unsupported_claims || [];
          if (claims.length) body += `<details class="pt flagged"><summary>${claims.length} claim${claims.length === 1 ? "" : "s"} flagged as unsupported</summary><ul class="claims">${claims.map((c) => "<li>" + esc(String(c)) + "</li>").join("")}</ul></details>`;
          if (d.reasoning) body += `<details class="pt"><summary>judge's reasoning</summary><p class="md">${highlightNums(esc(d.reasoning))}</p></details>`;
          if (d.verdict) body += `<details class="pt"><summary>interim verdict (before review)</summary><div class="verdict">${verdictHtml(d.verdict)}</div></details>`;
        }
        return card("judge", `<h3><span class="who">judge</span> ${roundTag(d)} <span class="pill ${d.converged ? "yes" : ""}">${d.converged ? "converged" : "not converged"}</span>${d.truncated ? ' <span class="pill live" title="the judge reply hit the output cap twice; fields recovered from the partial reply">reply truncated</span>' : ""}</h3>${body}`);
      },
      route(d) {
        card("route", (d.decision === "continue" ? "↻ " : "→ ") + esc(d.reason));
        if (d.decision === "continue") ctx.round = (d.round || ctx.round) + 1;
        setPending(d.decision === "continue" ? "bull analyst is drafting a rebuttal…" : "re-auditing flagged claims against the evidence packet…");
      },
      news_verify(d) {
        clearPending();
        let body = "";
        if (d.claim_reviews && d.claim_reviews.length) body += `<ul class="claims">${d.claim_reviews.map((r) => { const v = r.verdict || ({ supported: "flag overturned", contradicted: "flag upheld" }[r.status] || r.status || "?"); return `<li><span class="pill ${/overturned/.test(v) ? "yes" : /upheld/.test(v) ? "no" : ""}">${esc(v)}</span> ${esc(r.claim || "")}${r.evidence_summary ? " — " + esc(r.evidence_summary) : ""}</li>`; }).join("")}</ul>`;
        else body += `<p class="note">${esc(d.reasoning || "No unsupported claims to re-audit.")}</p>`;
        if (d.verdict_changed) body += `<p class="note">The re-audit revised the verdict.</p>`;
        if (d.leads) body += `<details class="pt"><summary>related news leads (display only, never evidence)</summary><pre class="evidence">${esc(d.leads)}</pre></details>`;
        const c = card("news", `<h3><span class="who">claim review</span></h3>${body}${d.recorded === false ? note(d) : ""}`);
        setPending("review governor is tracing every number in the verdict…"); return c;
      },
      review(d) {
        clearPending();
        const s = d.stats || {}; const tagged = taggedNumbers(d.verdict);
        return card("review", `<h3><span class="who">governed verdict</span> <span class="pill">${esc(s.status || "")}</span>${d.budget_exceeded ? ' <span class="pill live">budget exceeded</span>' : ""}</h3>
          <div class="verdict">${verdictHtml(d.verdict)}</div>
          ${kvs([["claims", num(s.claims)], ["cited", num(s.cited)], ["flagged", num(s.flagged)], ["derived", num(s.derived)], ["labeled", num(s.labeled)], ["annotated", num(s.annotated)]])}
          ${tagged.length ? `<div class="tagged"><span class="k">left tagged — no evidence anchor:</span> ${tagged.map((t) => `<span class="ungrounded">${esc(t)}</span>`).join(" ")}</div>` : `<div class="tagged"><span class="k">every number in the verdict traced to the evidence packet${s.sources_unavailable ? `; ${s.sources_unavailable} source${s.sources_unavailable === 1 ? "" : "s"} unavailable (see data gaps)` : ""}.</span></div>`}
          ${note(d)}`);
      },
      done(d) {
        clearPending();
        const u = d.usage || {};
        const cached = (u.cache_read || 0) + (u.cache_write || 0);
        const by = d.by_node || {}; const nodes = Object.keys(by);
        card("route", `done — ${d.rounds} round(s), ${d.converged ? "converged" : "not converged"}. ${num(u.calls)} model calls · ${num((u.input || 0) + cached)} input tokens${cached ? ` (${num(u.cache_read || 0)} read from cache)` : ""} · ${num(u.output)} output tokens.` +
          (nodes.length ? `<details class="pt tok"><summary>tokens by node</summary><table class="pkt"><thead><tr><th>node</th><th>calls</th><th>input</th><th>cache write</th><th>cache read</th><th>output</th></tr></thead><tbody>${nodes.map((n) => `<tr><td>${esc(n)}</td><td>${num(by[n].calls)}</td><td>${num(by[n].input)}</td><td>${num(by[n].cache_write)}</td><td>${num(by[n].cache_read)}</td><td>${num(by[n].output)}</td></tr>`).join("")}</tbody></table></details>` : ""));
        if (hooks.onEnd) hooks.onEnd("done", d);
      },
      saved(d) { if (hooks.onSaved) hooks.onSaved(d); },
      error(d) { clearPending(); card("error", `<h3><span class="who">error</span> ${esc(d.stage || "")}</h3><p>${esc(d.message || "unknown error")}</p>`); if (hooks.onEnd) hooks.onEnd("error", d); },
    };
    // public render API: with reveal on, each event waits its turn and types out;
    // a turn_id already painted is skipped (dedupe by turn id)
    const render = {};
    const fresh = (d) => { const id = d && d.turn_id; if (!id) return true; if (seen.has(id)) return false; seen.add(id); return true; };
    for (const name of Object.keys(paint)) {
      render[name] = (d) => { if (!fresh(d)) return; return reveal ? enqueue(async () => { const c = paint[name](d); if (c && c.classList) await revealCard(c); }) : paint[name](d); };
    }
    function clear() { el.innerHTML = ""; pending = null; generation += 1; queue = Promise.resolve(); seen = new Set(); }
    function renderAll(events) { const was = reveal; reveal = false; clear(); for (const e of events || []) { const fn = paint[e.event]; if (fn && fresh(e.data || {})) fn(e.data || {}); } clearPending(); reveal = was; }
    function setReveal(on, ms) { reveal = !!on; if (ms) charMs = ms; }
    return { el, card, setPending, clearPending, clear, render, renderAll, setReveal };
  }

  /* play(url, timeline): open an SSE stream and feed every event to the timeline. Returns {close}.
     Two kinds of "error" reach us: the SERVER's `event: error` (has JSON data — a debate problem) and the
     browser's native EventSource error (no data — the connection itself failed: auth, proxy, network). */
  function play(url, timeline, hooks = {}) {
    const source = new EventSource(url);
    let finished = false;
    const finish = () => { finished = true; source.close(); };
    const connectionLost = () => {
      if (finished) return;
      timeline.render.error({ stage: "connection", message:
        "The connection to the server dropped before the debate finished. If the run had started, it keeps " +
        "going on the server and will appear in the archive when it completes; reload in a minute or two." });
      finish(); if (hooks.onClose) hooks.onClose("error");
    };
    for (const name of Object.keys(timeline.render)) {
      source.addEventListener(name, (e) => {
        if (name === "error" && (e.data === undefined || e.data === null)) return connectionLost();
        let d = {}; try { d = JSON.parse(e.data); } catch (_) {}
        timeline.render[name](d);
        if (name === "done" || name === "error") { finish(); if (hooks.onClose) hooks.onClose(name); }
      });
    }
    source.onerror = connectionLost;
    return { close: finish, get finished() { return finished; } };
  }

  return { esc, num, md, verdictHtml, argumentHtml, splitArgument, packetHtml, taggedNumbers, Timeline, play };
})();
