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
  function packetHtml(evidence) {
    const lines = String(evidence || "").split("\n");
    const grid = []; let i = 0, section = null;
    for (; i < lines.length; i++) {
      const l = lines[i].trim(); let m;
      if (!l) continue;
      if ((m = l.match(/^\[([^\]]+)\]$/))) { section = { name: m[1], rows: [] }; grid.push(section); continue; }
      if ((m = l.match(/^-\s+([^:]+):\s+(.*?)\s*(?:\[source:\s*([^\]]+)\])?$/)) && section) { section.rows.push({ k: m[1], v: m[2], src: m[3] || "" }); continue; }
      if (/^\[.*unavailable.*\]$/.test(l) && section) continue;
      break;                                                        // first line that is not market data
    }
    const rest = lines.slice(i).join("\n").trim();
    const gridHtml = grid.filter((s) => s.rows.length).map((s) => `<div class="pk"><div class="pkh">${esc(s.name.toLowerCase())}</div><div class="pkg">` +
      s.rows.map((r) => `<div class="pkr" title="${esc(r.src)}"><span class="pkk">${esc(r.k)}</span><span class="pkv">${highlightNums(esc(r.v))}</span></div>`).join("") + `</div></div>`).join("");
    return (gridHtml || "") + (rest ? `<details class="pt"><summary>filings &amp; news evidence (${num(rest.length)} chars)</summary><pre class="evidence">${esc(rest)}</pre></details>` : "") +
      (!gridHtml ? `<pre class="evidence">${esc(evidence || "")}</pre>` : "");
  }

  const kvs = (pairs) => '<div class="kv">' + pairs.map(([k, v]) => `<div><div class="k">${k}</div><div class="v">${v}</div></div>`).join("") + "</div>";
  const note = (d) => (d && d.note ? `<p class="note">${esc(d.note)}</p>` : "");

  /* Timeline(el, hooks): renders events into el.
     hooks: onStart(d), onEnd(kind, d), onSaved(d), autoscroll (bool), reveal (bool: read-along pacing), charMs
     With reveal on, renders are queued and each card's summary lines type out at reading pace before the next card. */
  function Timeline(el, hooks = {}) {
    let pending = null, maxRounds = 0, queue = Promise.resolve(), reveal = !!hooks.reveal, charMs = hooks.charMs || 9;
    const ctx = { round: 0 };
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
    const enqueue = (fn) => { queue = queue.then(fn).catch(() => {}); return queue; };

    const paint = {
      start(d) { maxRounds = d.max_rounds || 0; ctx.round = 0; if (hooks.onStart) hooks.onStart(d); if (d.note) card("route", esc(d.note)); setPending("research agent is assembling the evidence packet (market, SEC filings, news)…"); },
      research(d) {
        clearPending();
        const c = card("research", `<h3><span class="who">research</span> evidence packet</h3>${packetHtml(d.evidence)}`);
        ctx.round = 1; setPending("bull analyst is building the upside case…"); return c;
      },
      bull(d) { clearPending(); ctx.round = d.round || ctx.round; const c = card("bull", `<h3><span class="who">bull</span> ${roundTag(d)}</h3>${argumentHtml(d.text)}`); setPending("bear analyst is building the downside case…"); return c; },
      bear(d) { clearPending(); ctx.round = d.round || ctx.round; const c = card("bear", `<h3><span class="who">bear</span> ${roundTag(d)}</h3>${argumentHtml(d.text)}`); setPending("judge is weighing both sides against the evidence…"); return c; },
      judge(d) {
        clearPending(); ctx.round = d.round || ctx.round;
        let body = "";
        if (d.recorded === false) body = note(d);
        else {
          body += `<div class="sc"><div class="scb"><div class="k">strongest bull point</div><div class="v">${highlightNums(esc(d.bull_strongest || "—"))}</div></div><div class="scr"><div class="k">strongest bear point</div><div class="v">${highlightNums(esc(d.bear_strongest || "—"))}</div></div></div>`;
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
        if (d.claim_reviews && d.claim_reviews.length) body += `<ul class="claims">${d.claim_reviews.map((r) => `<li><span class="pill">${esc(r.status || "?")}</span> ${esc(r.claim || "")}${r.evidence_summary ? " — " + esc(r.evidence_summary) : ""}</li>`).join("")}</ul>`;
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
          ${tagged.length ? `<div class="tagged"><span class="k">left tagged — no evidence anchor:</span> ${tagged.map((t) => `<span class="ungrounded">${esc(t)}</span>`).join(" ")}</div>` : `<div class="tagged"><span class="k">every number in the verdict traced to the evidence packet.</span></div>`}
          ${note(d)}`);
      },
      done(d) {
        clearPending();
        const u = d.usage || {};
        const cached = (u.cache_read || 0) + (u.cache_write || 0);
        card("route", `done — ${d.rounds} round(s), ${d.converged ? "converged" : "not converged"}. ${num(u.calls)} model calls · ${num((u.input || 0) + cached)} input tokens${cached ? ` (${num(u.cache_read || 0)} read from cache)` : ""} · ${num(u.output)} output tokens.`);
        if (hooks.onEnd) hooks.onEnd("done", d);
      },
      saved(d) { if (hooks.onSaved) hooks.onSaved(d); },
      error(d) { clearPending(); card("error", `<h3><span class="who">error</span> ${esc(d.stage || "")}</h3><p>${esc(d.message || "unknown error")}</p>`); if (hooks.onEnd) hooks.onEnd("error", d); },
    };
    // public render API: with reveal on, each event waits its turn and types out
    const render = {};
    for (const name of Object.keys(paint)) {
      render[name] = (d) => reveal ? enqueue(async () => { const c = paint[name](d); if (c && c.classList) await revealCard(c); }) : paint[name](d);
    }
    function clear() { el.innerHTML = ""; pending = null; queue = Promise.resolve(); }
    function renderAll(events) { const was = reveal; reveal = false; clear(); for (const e of events || []) { const fn = paint[e.event]; if (fn) fn(e.data || {}); } clearPending(); reveal = was; }
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
