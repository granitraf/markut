/* Markut shared front-end: mini-markdown, verdict highlighting, the debate
   timeline (one card per agent event) and the SSE player. No dependencies,
   no CDN — the pages work offline. Exposes window.Markut. */
window.Markut = (function () {
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const num = (n) => (n == null ? "–" : Number(n).toLocaleString());

  // ---- mini-markdown: headings, lists (-, *, 1.), bold/italic/code, links, hr, quotes, fenced code, tables ----
  function inline(s) {
    return esc(s)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|\W)\*(?!\s)(.+?)\*(?=\W|$)/g, "$1<em>$2</em>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+|\/[^\s)]*|#[^\s)]*)\)/g, '<a href="$2" rel="noopener">$1</a>');
  }
  function md(text) {
    const out = []; let list = null, listTag = "ul", para = [], code = null, table = null;
    const flushP = () => { if (para.length) { out.push("<p>" + inline(para.join(" ")) + "</p>"); para = []; } };
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
        if (cells.every((c) => /^:?-{2,}:?$/.test(c))) continue;             // separator row
        const tag = table ? "td" : "th";
        (table = table || []).push("<tr>" + cells.map((c) => `<${tag}>${inline(c)}</${tag}>`).join("") + "</tr>");
        continue;
      }
      flushT();
      if (!line.trim()) { flushP(); flushL(); continue; }
      if (/^(-{3,}|\*{3,})$/.test(line.trim())) { flushP(); flushL(); out.push("<hr>"); continue; }
      if ((m = line.match(/^(#{1,6})\s+(.*)$/))) { flushP(); flushL(); const h = Math.min(m[1].length, 4); out.push(`<h${h}>${inline(m[2])}</h${h}>`); continue; }
      if ((m = line.match(/^\s*[-*•]\s+(.*)$/))) { flushP(); if (list && listTag !== "ul") flushL(); listTag = "ul"; (list = list || []).push("<li>" + inline(m[1]) + "</li>"); continue; }
      if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) { flushP(); if (list && listTag !== "ol") flushL(); listTag = "ol"; (list = list || []).push("<li>" + inline(m[1]) + "</li>"); continue; }
      if ((m = line.match(/^>\s?(.*)$/))) { flushP(); flushL(); out.push("<blockquote>" + inline(m[1]) + "</blockquote>"); continue; }
      flushL(); para.push(line);
    }
    if (code !== null) out.push("<pre><code>" + esc(code.join("\n")) + "</code></pre>");
    flushP(); flushL(); flushT();
    return out.join("");
  }
  // the governor's inline tag: "<number> [UNGROUNDED — no evidence anchor]" -> highlighted span
  function verdictHtml(text) {
    let html = esc(text || "");
    html = html.replace(/(\S+)\s\[UNGROUNDED — no evidence anchor\]/g, '<span class="ungrounded">$1 <small>UNGROUNDED · no evidence anchor</small></span>');
    return html.split(/\n{2,}/).map((p) => "<p>" + p.replace(/\n/g, "<br>") + "</p>").join("");
  }
  const kvs = (pairs) => '<div class="kv">' + pairs.map(([k, v]) => `<div><div class="k">${k}</div><div class="v">${v}</div></div>`).join("") + "</div>";
  const note = (d) => (d && d.note ? `<p class="note">${esc(d.note)}</p>` : "");
  const roundTag = (d) => (d.round ? `<span class="round">round ${d.round}</span>` : "");

  /* Timeline(el, hooks): renders events into el. hooks: onStart(d), onEnd(kind, d), onSaved(d), onStatus(html) */
  function Timeline(el, hooks = {}) {
    let pending = null;
    function card(cls, html) {
      const c = document.createElement("div"); c.className = "card " + cls; c.innerHTML = html;
      el.appendChild(c); if (c.scrollIntoView && hooks.autoscroll !== false) c.scrollIntoView({ block: "nearest", behavior: "smooth" }); return c;
    }
    function clearPending() { if (pending) { pending.remove(); pending = null; } }
    function setPending(label) { clearPending(); pending = card("pending", `<span class="dot"></span>${esc(label)}`); }
    const render = {
      start(d) { if (hooks.onStart) hooks.onStart(d); if (d.note) card("route", esc(d.note)); setPending("Research agent is assembling the evidence packet (market, SEC filings, news)…"); },
      research(d) {
        clearPending();
        card("research", `<h3><span class="who">Research</span> evidence packet</h3>
          <details><summary>Show the ${num((d.evidence || "").length)}-character packet every agent sees</summary><pre class="evidence">${esc(d.evidence || "")}</pre></details>`);
        setPending("Bull analyst is building the upside case…");
      },
      bull(d) { clearPending(); card("bull", `<h3><span class="who">Bull</span> ${roundTag(d)}</h3><div class="md">${md(d.text)}</div>`); setPending("Bear analyst is building the downside case…"); },
      bear(d) { clearPending(); card("bear", `<h3><span class="who">Bear</span> ${roundTag(d)}</h3><div class="md">${md(d.text)}</div>`); setPending("Judge is weighing both sides against the evidence…"); },
      judge(d) {
        clearPending();
        let body = "";
        if (d.recorded === false) body = note(d);
        else {
          body += d.bull_strongest ? `<p><strong>Strongest bull point:</strong> ${esc(d.bull_strongest)}</p>` : "";
          body += d.bear_strongest ? `<p><strong>Strongest bear point:</strong> ${esc(d.bear_strongest)}</p>` : "";
          if (d.unsupported_claims && d.unsupported_claims.length) body += `<p><strong>Flagged as unsupported:</strong></p><ul class="claims">${d.unsupported_claims.map((c) => "<li>" + esc(String(c)) + "</li>").join("")}</ul>`;
          body += d.reasoning ? `<p class="note">${esc(d.reasoning)}</p>` : "";
          body += d.verdict ? `<details><summary>Interim verdict (before review)</summary><div class="verdict">${verdictHtml(d.verdict)}</div></details>` : "";
        }
        card("judge", `<h3><span class="who">Judge</span> ${roundTag(d)} <span class="pill">${d.converged ? "converged" : "not converged"}</span></h3>${body}`);
      },
      route(d) {
        card("route", (d.decision === "continue" ? "↻ " : "→ ") + esc(d.reason));
        setPending(d.decision === "continue" ? "Bull analyst is drafting a rebuttal…" : "Re-auditing flagged claims against the evidence packet…");
      },
      news_verify(d) {
        clearPending();
        let body = "";
        if (d.claim_reviews && d.claim_reviews.length) body += `<ul class="claims">${d.claim_reviews.map((r) => `<li><span class="pill">${esc(r.status || "?")}</span> ${esc(r.claim || "")}${r.evidence_summary ? " — " + esc(r.evidence_summary) : ""}</li>`).join("")}</ul>`;
        else body += `<p>${esc(d.reasoning || "No unsupported claims to re-audit.")}</p>`;
        if (d.verdict_changed) body += `<p class="note">The re-audit revised the verdict.</p>`;
        if (d.leads) body += `<details><summary>Related news leads (display only, never evidence)</summary><pre class="evidence">${esc(d.leads)}</pre></details>`;
        card("news", `<h3><span class="who">Claim review</span></h3>${body}${d.recorded === false ? note(d) : ""}`);
        setPending("Review governor is tracing every number in the verdict…");
      },
      review(d) {
        clearPending();
        const s = d.stats || {};
        card("review", `<h3><span class="who">Governed verdict</span> <span class="pill">${esc(s.status || "")}</span>${d.budget_exceeded ? ' <span class="pill live">budget exceeded</span>' : ""}</h3>
          <div class="verdict">${verdictHtml(d.verdict)}</div>
          ${kvs([["claims", num(s.claims)], ["cited", num(s.cited)], ["flagged", num(s.flagged)], ["derived", num(s.derived)], ["labeled", num(s.labeled)], ["annotated", num(s.annotated)]])}
          ${note(d)}`);
      },
      done(d) {
        clearPending();
        const u = d.usage || {};
        card("route", `Done — ${d.rounds} round(s), ${d.converged ? "converged" : "not converged"}. ${num(u.calls)} model calls · ${num(u.input)} input tokens · ${num(u.output)} output tokens.`);
        if (hooks.onEnd) hooks.onEnd("done", d);
      },
      saved(d) { if (hooks.onSaved) hooks.onSaved(d); },
      error(d) { clearPending(); card("error", `<h3><span class="who">Error</span> ${esc(d.stage || "")}</h3><p>${esc(d.message || "unknown error")}</p>`); if (hooks.onEnd) hooks.onEnd("error", d); },
    };
    function clear() { el.innerHTML = ""; pending = null; }
    function renderAll(events) { clear(); for (const e of events || []) { const fn = render[e.event]; if (fn) fn(e.data || {}); } clearPending(); }
    return { el, card, setPending, clearPending, clear, render, renderAll };
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
        if (name === "error" && (e.data === undefined || e.data === null)) return connectionLost();   // native
        let d = {}; try { d = JSON.parse(e.data); } catch (_) {}
        timeline.render[name](d);
        if (name === "done" || name === "error") { finish(); if (hooks.onClose) hooks.onClose(name); }
      });
    }
    source.onerror = connectionLost;
    return { close: finish, get finished() { return finished; } };
  }

  return { esc, num, md, verdictHtml, Timeline, play };
})();
