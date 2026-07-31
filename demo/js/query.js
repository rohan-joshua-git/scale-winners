/**
 * Query console: a natural-language front end that now genuinely talks to
 * two things.
 *
 * 1. Backlog analytics ("how many", "highest priority", "by region") go to
 *    the live Stage 5a service (pipeline/stats, mounted in
 *    pipeline/rag/service.py's POST /chat) when it's reachable. No LLM
 *    computes a number there -- the model only fills a whitelisted
 *    QuerySpec; a deterministic engine runs it over the real ranked
 *    backlog. That's real data, not a mock.
 * 2. Everything else (the three demo cases, "how does this work",
 *    greetings) is answered locally from js/data.js, same as before --
 *    those case IDs only exist in this page's mock data, not the live
 *    tenant, so routing them to the API would just bounce off /chat's
 *    "explanatory" route with nothing useful to show.
 *
 * If the API isn't running, backlog questions get an honest "start it with
 * `python main.py serve`" instead of a wrong-looking mock answer.
 */
(function () {
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // pipeline/rag/service.py, started via `python main.py serve` (default
  // port 8010) or `uvicorn pipeline.rag.service:app --port 8010`.
  const API_BASE = 'http://127.0.0.1:8010';
  const LIVE_TIMEOUT_MS = 6000;
  const HEALTH_TIMEOUT_MS = 3000;

  const fmtUsd = (n) =>
    new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 }).format(n);

  const escapeHtml = (str) =>
    String(str).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));

  // Safety net for live model prose: the system prompt (pipeline/stats/nl.py)
  // asks for plain sentences with no line breaks, but a raw <p>${escapeHtml(text)}</p>
  // would flatten any stray newline the model still emits into a wall of text.
  // Escape first, then turn blank-line breaks into paragraphs and any
  // remaining single newline into a soft break.
  function textToHtml(text) {
    return String(text)
      .split(/\n\s*\n/)
      .map((block) => escapeHtml(block.trim()).replace(/\n/g, '<br>'))
      .filter((b) => b.length)
      .map((b) => `<p>${b}</p>`)
      .join('');
  }

  async function fetchJson(url, options, timeoutMs) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const res = await fetch(url, { ...options, signal: controller.signal });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return await res.json();
    } finally {
      clearTimeout(timer);
    }
  }

  // Hardcoded, not model-routed: a bare case number, or "case"/"alert" plus
  // one, always means "look this alert up" -- GET /stats/alert/{id}, a
  // direct pandas lookup with no LLM anywhere (pipeline/stats/engine.py's
  // alert() bypasses the QuerySpec surface on purpose, since ALERT_ID isn't
  // a registered filterable field there).
  function extractCaseNumber(rawText) {
    const trimmed = rawText.trim();
    if (/^\d{3,7}$/.test(trimmed)) return trimmed;
    const m = trimmed.match(/\b(?:case|alert)(?:\s*number)?\s*#?\s*(\d{3,7})\b/i);
    return m ? m[1] : null;
  }

  function renderCaseSummary(payload) {
    const a = payload.alert;
    const score = Math.round((a.exposure ?? 0) * 100);
    const tier = a.ALERT_PRIORITY || 'MEDIUM';
    const amount = typeof a.AMOUNT_USD === 'number' ? fmtUsd(a.AMOUNT_USD) : 'not recorded';
    const gaugeId = `chat-gauge-${a.ALERT_ID}-${Math.random().toString(36).slice(2, 8)}`;
    const fact = (label, value) =>
      `<li><span>${escapeHtml(label)}</span><span>${escapeHtml(value == null ? 'not recorded' : String(value))}</span></li>`;

    return `
      <p>Alert <strong>${a.ALERT_ID}</strong> — exposure score ${score}/100, ${escapeHtml(tier)} priority,
      #${a.exposure_rank ?? '?'} of ${payload.provenance.population.toLocaleString()} in the backlog.</p>
      <div class="chat-case">
        <div class="chat-case__gauge-panel">
          <div class="gauge chat-case__gauge" data-gauge id="${gaugeId}">
            <svg data-gauge-svg viewBox="0 0 300 300" role="img" aria-label="Exposure score dial for alert ${a.ALERT_ID}">
              <path data-gauge-track class="gauge-track"></path>
              <path data-gauge-progress class="gauge-progress"></path>
              <g data-gauge-ticks></g>
              <line data-gauge-needle class="gauge-needle" x1="150" y1="112" x2="150" y2="44"></line>
              <circle class="gauge-hub" cx="150" cy="150" r="7"></circle>
            </svg>
            <div class="gauge__readout">
              <span class="gauge__score" data-gauge-score>0</span>
              <span class="gauge__scale">/ 100</span>
            </div>
            <p class="gauge__tier" data-gauge-tier aria-live="polite"></p>
          </div>
        </div>
        <ul class="chat-case__facts">
          ${fact('Type', a.ALERT_TYPE)}
          ${fact('Jurisdiction', a.DEST_COUNTRY_NAME ? `${a.DEST_COUNTRY_NAME} (${a.DEST_FATF_STATUS || 'n/a'})` : null)}
          ${fact('Amount', amount)}
          ${fact('Age', a.age_days != null ? `${a.age_days} days (${a.band || 'n/a'})` : null)}
          ${fact('Client region', a.CLIENT_REGION_CODE)}
          ${fact('Assigned to', a.ASSIGNED_TO || 'unassigned')}
          ${fact('SLA', a.SLA_BREACHED ? 'breached' : 'within SLA')}
        </ul>
      </div>
    `;
  }

  // Aggregate/backlog vocabulary from pipeline/stats/README.md's own
  // disambiguation rules -- if a question sounds like this, it belongs to
  // the live engine, not the three mocked cases.
  function isStatsQuestion(q) {
    return /(how many|count|breakdown|distribution|\bper\b|backlog|queue|highest priority|top \d|\bemea\b|\bapac\b|\bfatf\b|\bregion\b|ageing|aging|\bband\b|unassigned|unresolved|exposure)/.test(q);
  }

  function renderRowsTable(rows) {
    if (!rows || !rows.length) return '';
    const cols = Object.keys(rows[0]).slice(0, 5);
    const head = cols.map((c) => `<th>${escapeHtml(c)}</th>`).join('');
    const body = rows
      .map((r) => `<tr>${cols.map((c) => `<td>${escapeHtml(r[c] ?? '—')}</td>`).join('')}</tr>`)
      .join('');
    return `<div class="chat-table-wrap"><table class="chat-table">
      <thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
  }

  /**
   * Deterministic, code-computed stats -- not written by the LLM. Everything
   * here comes straight from resp.n_matched / resp.provenance / resp.rows,
   * the same numbers narrate() was told not to restate in prose.
   */
  function renderSummaryStats(resp) {
    const prov = resp.provenance || {};
    const rows = resp.rows || [];
    const stats = [];

    if (typeof resp.n_matched === 'number') {
      stats.push({ label: 'Matched', value: resp.n_matched.toLocaleString() });
    }
    if (typeof prov.population === 'number') {
      stats.push({ label: 'Backlog', value: prov.population.toLocaleString() });
      if (typeof resp.n_matched === 'number' && prov.population > 0) {
        stats.push({ label: '% of backlog', value: `${((resp.n_matched / prov.population) * 100).toFixed(1)}%` });
      }
    }
    const exposures = rows.map((r) => r.exposure ?? r.avg_exposure).filter((v) => typeof v === 'number');
    if (exposures.length) {
      const avg = exposures.reduce((a, b) => a + b, 0) / exposures.length;
      stats.push({ label: 'Avg exposure (shown rows)', value: avg.toFixed(3) });
    }

    if (!stats.length) return '';
    return `<div class="chat-stats">${stats.map((s) =>
      `<div class="chat-stats__item"><span class="chat-stats__value">${escapeHtml(s.value)}</span>`
      + `<span class="chat-stats__label">${escapeHtml(s.label)}</span></div>`).join('')}</div>`;
  }

  /** Recommended action + expected effect -- also code-carried, not just
   * prose: both come straight through from the API's `recommended_action` /
   * `expected_effect` fields (pipeline/stats/nl.py's Narration), which the
   * system prompt constrains to restating numbers already in the payload
   * rather than projecting a new outcome. */
  function renderActions(resp) {
    if (!resp.recommended_action && !resp.expected_effect) return '';
    const rows = [];
    if (resp.recommended_action) {
      rows.push(`<div class="chat-actions__row"><p class="chat-actions__label">Recommended action</p>`
        + `<p class="chat-actions__text">${escapeHtml(resp.recommended_action)}</p></div>`);
    }
    if (resp.expected_effect) {
      rows.push(`<div class="chat-actions__row"><p class="chat-actions__label">Expected effect</p>`
        + `<p class="chat-actions__text">${escapeHtml(resp.expected_effect)}</p></div>`);
    }
    return `<div class="chat-actions">${rows.join('')}</div>`;
  }

  /** Query spec + assumptions + extraction method -- provenance for the
   * answer above, kept small and pushed to the very bottom rather than
   * competing with the answer, the numbers, or the recommendation. */
  function renderMeta(resp) {
    const bits = [];
    if (resp.spec_english) bits.push(`query: <em>${escapeHtml(resp.spec_english)}</em>`);
    if (resp.extraction_method) bits.push(`extraction: ${escapeHtml(resp.extraction_method)}`);
    let html = bits.length ? `<p class="chat-meta">${bits.join(' · ')}</p>` : '';
    if (resp.assumptions && resp.assumptions.length) {
      html += `<p class="chat-meta">Assumption: ${resp.assumptions.map(escapeHtml).join('; ')}</p>`;
    }
    return html;
  }

  function renderLiveAnswer(resp) {
    const parts = [textToHtml(resp.answer)];

    if (resp.route === 'analytical') {
      parts.push(renderSummaryStats(resp));
      if (resp.rows && resp.rows.length) parts.push(renderRowsTable(resp.rows.slice(0, 6)));
      parts.push(renderActions(resp));
      parts.push(renderMeta(resp));
    } else if (resp.route === 'explanatory') {
      parts.push('<p class="chat-meta">Routed to Stage 4 (GraphRAG grounding) — this console only shows Stage 5a backlog analytics live.</p>');
    } else if (resp.assumptions && resp.assumptions.length) {
      parts.push(`<p class="chat-meta">${resp.assumptions.map(escapeHtml).join('; ')}</p>`);
    }

    return parts.join('');
  }

  function renderOffline() {
    return `<p>The live backlog analytics service isn't reachable from this static
      page right now. Start it with <code>python main.py serve</code> from the repo
      root, then ask again — or try one of the case prompts below, which work offline.</p>`;
  }

  const KEYWORDS = {
    sanctions: ['sanction', 'iran', 'tehran', 'istanbul', '88214', 'correspondent'],
    layering: ['layering', 'singapore', 'frankfurt', 'garch', 'velocity', '90067', 'baseline'],
    shell: ['shell', 'bvi', 'virgin islands', 'ownership', 'ubo', 'beneficial', 'network', '91325', 'graph', 'london'],
  };

  const SUGGESTIONS = [
    'Why was TXN-88214-KL flagged Critical?',
    'Show the shell company ownership network',
    'How many unresolved alerts are in EMEA, by FATF status?',
    'Give me the highest-priority cases in the backlog',
    'Look up case 18324',
    'How does TrustSphere write its rationale?',
  ];

  const GREETING =
    "I'm the query console. Ask about one of the three example cases below and " +
    "I'll answer from the same data the dashboard uses. Ask a backlog question " +
    '("how many", "highest priority", "by region") and — if the Stage 5a service ' +
    "is running — I'll query the real ranked backlog live, no mock involved. Or " +
    'give me a bare case number ("case 18324") for a full summary and score dial.';

  function findCase(q) {
    const hit = Object.entries(KEYWORDS).find(([, words]) => words.some((w) => q.includes(w)));
    return hit ? TRUSTSPHERE_CASES.find((c) => c.id === hit[0]) : null;
  }

  function isGreeting(q) {
    return /^(hi|hello|hey|help|what can you do|start)\b/.test(q.trim());
  }

  function isPipelineQuestion(q) {
    return /(how does|how do|pipeline|\brag\b|rationale generat|explainab|joule)/.test(q);
  }

  function followUp(q, caseData) {
    if (!caseData) return null;
    if (/amount|how much|value|worth/.test(q)) {
      const t = caseData.transaction;
      return `${t.id} moved ${fmtUsd(t.amountUsd)} from ${t.originator} (${t.originatingCountry}) `
        + `to ${t.beneficiary} (${t.destinationCountry}).`;
    }
    if (/action|next step|recommend|do about/.test(q)) {
      return caseData.rationale[caseData.rationale.length - 1];
    }
    if (/score|tier/.test(q)) {
      return caseData.hardOverride
        ? `Composite score was ${caseData.preOverrideScore} / 100, but a sanctions hit is a hard `
          + `override — it routes straight to ${caseData.tier} regardless of the score.`
        : `Composite score ${caseData.score} / 100, landing in the ${caseData.tier} tier.`;
    }
    return null;
  }

  // Compact schematic layout for the mini ownership graph rendered inside a
  // chat card -- a scaled-down copy of js/graph.js's LAYOUT (same node ids,
  // same relative shape), but static: no scroll trigger, since a chat bubble
  // has no scroll journey to scrub against. Only the 'shell' case in
  // data.js carries `.graph`, so this only ever renders there.
  const CASE_GRAPH_LAYOUT = {
    originator: { x: 56, y: 40 },
    beneficiary: { x: 138, y: 66 },
    shell2: { x: 193, y: 38 },
    shell3: { x: 272, y: 66 },
    shell4: { x: 285, y: 132 },
    ubo: { x: 180, y: 168 },
  };
  const CASE_GRAPH_RADIUS = { originator: 5, shell: 5, ubo: 7 };
  const CASE_GRAPH_HINT = 'Hover or tap a node to trace its ownership link.';

  // Alert 18324 is the backlog's #1-exposure case in the live demo tenant;
  // pairing it with the mocked shell-company graph gives the "look up a case
  // number" path something to show without pretending the live backlog has
  // real graph-traversal data behind every alert.
  const HARDCODED_CASE_GRAPH_ID = '18324';

  function renderCaseGraphMarkup(caseData) {
    if (!caseData.graph) return '';
    return `
      <div class="chat-graph" data-chat-graph>
        <svg data-chat-graph-svg viewBox="0 0 340 190" role="img"
          aria-label="Beneficial-ownership graph for ${escapeHtml(caseData.transaction.id)}"></svg>
        <p class="chat-graph__caption" data-chat-graph-caption>${CASE_GRAPH_HINT}</p>
      </div>
    `;
  }

  function renderCaseCard(caseData) {
    const t = caseData.transaction;
    const top = [...caseData.factors].sort((a, b) => b.contribution - a.contribution).slice(0, 3);
    return `
      <p>Here's what the pipeline has on <strong>${t.id}</strong> (${escapeHtml(caseData.corridor)}):</p>
      <div class="chat-card">
        <div class="chat-card__head">
          <span class="chat-card__id">${t.id}</span>
          <span class="tier-badge tier-${caseData.tier.toLowerCase()}">${caseData.tier}</span>
        </div>
        <p class="chat-card__corridor">${escapeHtml(caseData.corridor)} · ${fmtUsd(t.amountUsd)}</p>
        <ol class="chat-card__rationale">
          ${caseData.rationale.map((r) => `<li>${escapeHtml(r)}</li>`).join('')}
        </ol>
        <ul class="chat-card__factors">
          ${top.map((f) => `<li><span>${escapeHtml(f.label)}</span>`
            + `<span class="chat-card__factor-score">${Math.round(f.contribution * 100)}</span></li>`).join('')}
        </ul>
        ${renderCaseGraphMarkup(caseData)}
      </div>
    `;
  }

  function renderPipelineAnswer() {
    return `
      <p>Every case here traces the same path: ingestion → data-quality checks →
      SAP Datasphere federation → AHP + Isolation Forest + EWMA + GARCH scoring →
      RAG context retrieval → Joule explainability → dashboards → human review.</p>
      <p>This console sits on top of the explainability phase — it turns the score,
      factors, and retrieved context into an answer, but it never re-decides the tier.
      See the <a href="index.html#pipeline-heading">full pipeline walkthrough</a>.</p>
    `;
  }

  function renderFallback(query) {
    return `<p>I don't have a case matching “${escapeHtml(query)}” yet. This console only
      knows the three cases the dashboard uses — try one of the prompts below.</p>`;
  }

  class TrustSphereQueryConsole {
    constructor(root) {
      this.root = root;
      this.log = root.querySelector('[data-chat-log]');
      this.suggestionsList = root.querySelector('[data-chat-suggestions-list]');
      this.form = root.querySelector('[data-chat-form]');
      this.input = root.querySelector('[data-chat-input]');
      this.submitBtn = root.querySelector('[data-chat-submit]');
      this.statusEl = root.querySelector('[data-chat-status]');
      this.statusText = root.querySelector('[data-chat-status-text]');
      this.lastCase = null;
      this.live = null; // null = unknown/checking, then true/false

      this.renderSuggestions();
      this.appendAssistant(`<p>${GREETING}</p>`);
      this.checkStatus();

      this.form.addEventListener('submit', (e) => {
        e.preventDefault();
        this.handleSend(this.input.value);
      });
    }

    setStatus(state, text) {
      if (!this.statusEl) return;
      this.statusEl.dataset.state = state;
      if (this.statusText) this.statusText.textContent = text;
    }

    async checkStatus() {
      try {
        const health = await fetchJson(`${API_BASE}/stats/health`, {}, HEALTH_TIMEOUT_MS);
        this.live = true;
        this.setStatus('live', `Live — connected to the Stage 5a backlog service `
          + `(${health.backlog} unresolved alerts, as of ${health.as_of}).`);
      } catch (err) {
        this.live = false;
        this.setStatus('offline', 'Offline — Stage 5a service not reachable. '
          + 'Backlog questions fall back to a note; case prompts still work.');
      }
    }

    renderSuggestions() {
      this.suggestionsList.innerHTML = '';
      SUGGESTIONS.forEach((text) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'chat-chip';
        btn.textContent = text;
        btn.addEventListener('click', () => this.handleSend(text));
        this.suggestionsList.appendChild(btn);
      });
    }

    appendUser(text) {
      const msg = document.createElement('div');
      msg.className = 'chat-msg chat-msg--user';
      msg.innerHTML = '<span class="chat-msg__role">You</span><div class="chat-msg__bubble"></div>';
      msg.querySelector('.chat-msg__bubble').textContent = text;
      this.log.appendChild(msg);
      this.animateIn(msg);
      this.scrollToEnd();
    }

    appendAssistant(html) {
      const msg = document.createElement('div');
      msg.className = 'chat-msg chat-msg--assistant';
      msg.innerHTML = `<span class="chat-msg__role">TrustSphere</span><div class="chat-msg__bubble">${html}</div>`;
      this.log.appendChild(msg);
      this.animateIn(msg);
      this.scrollToEnd();
      return msg;
    }

    appendTyping() {
      const msg = document.createElement('div');
      msg.className = 'chat-msg chat-msg--assistant';
      msg.innerHTML = '<span class="chat-msg__role">TrustSphere</span>'
        + '<div class="chat-msg__bubble"><span class="chat-typing"><span></span><span></span><span></span></span></div>';
      this.log.appendChild(msg);
      this.scrollToEnd();
      return msg;
    }

    animateIn(msg) {
      if (reduceMotion) return;
      gsap.fromTo(msg, { opacity: 0, y: 8 }, { opacity: 1, y: 0, duration: 0.35, ease: 'power2.out' });
    }

    scrollToEnd() {
      this.log.scrollTop = this.log.scrollHeight;
    }

    /**
     * Case matches, greetings, and the pipeline explainer stay local (those
     * case IDs live only in js/data.js). Anything that reads as a backlog
     * question goes to the live Stage 5a service when reachable -- that's
     * the actual integration point for pipeline/stats.
     */
    async resolveAnswer(rawText) {
      const q = rawText.toLowerCase();
      if (isGreeting(q)) return { html: `<p>${GREETING}</p>`, caseData: null };
      if (isPipelineQuestion(q)) return { html: renderPipelineAnswer(), caseData: null };

      const matchedCase = findCase(q);
      if (matchedCase) {
        this.lastCase = matchedCase;
        return { html: renderCaseCard(matchedCase), caseData: matchedCase };
      }

      if (isStatsQuestion(q)) {
        if (this.live === false) return { html: renderOffline(), caseData: null };
        try {
          const resp = await fetchJson(`${API_BASE}/chat`, {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ message: rawText, use_llm: true, narrate: true }),
          }, LIVE_TIMEOUT_MS);
          this.live = true;
          this.setStatus('live', 'Live — that answer came from the Stage 5a backlog service.');
          return { html: renderLiveAnswer(resp), caseData: null };
        } catch (err) {
          this.live = false;
          this.setStatus('offline', "Offline — that backlog question couldn't reach the Stage 5a service.");
          return { html: renderOffline(), caseData: null };
        }
      }

      const follow = followUp(q, this.lastCase);
      if (follow) return { html: `<p>${escapeHtml(follow)}</p>`, caseData: null };

      return { html: renderFallback(rawText), caseData: null };
    }

    /** Hardcoded case-number path: GET /stats/alert/{id}, no LLM, no spec
     * extraction. Returns { html, alert, graphCase } -- alert is non-null only
     * on a successful lookup (wires up a live gauge); graphCase is non-null
     * only for alert 18324, which is paired with the same hardcoded
     * shell-company ownership graph as the mocked TXN-91325-FZ case (the live
     * backlog has no graph-traversal data of its own, so this is a fixed
     * demo pairing, not a real lookup). */
    async resolveCaseLookup(caseNumber) {
      if (this.live === false) {
        return { html: renderOffline(), alert: null, graphCase: null };
      }
      try {
        const payload = await fetchJson(`${API_BASE}/stats/alert/${caseNumber}`, {}, LIVE_TIMEOUT_MS);
        this.live = true;
        this.setStatus('live', 'Live — that lookup came from the Stage 5a backlog service.');
        const graphCase = caseNumber === HARDCODED_CASE_GRAPH_ID
          ? TRUSTSPHERE_CASES.find((c) => c.id === 'shell')
          : null;
        const html = renderCaseSummary(payload) + (graphCase ? renderCaseGraphMarkup(graphCase) : '');
        return { html, alert: payload.alert, graphCase };
      } catch (err) {
        if (err && err.message === 'HTTP 404') {
          return {
            html: `<p>Alert ${escapeHtml(caseNumber)} isn't in the unresolved backlog — it may `
              + 'already be resolved, or the number doesn\'t exist. Try "case 18324" instead.</p>',
            alert: null,
            graphCase: null,
          };
        }
        this.live = false;
        this.setStatus('offline', "Offline — that case lookup couldn't reach the Stage 5a service.");
        return { html: renderOffline(), alert: null, graphCase: null };
      }
    }

    /** Instantiate a real TrustSphereGauge (js/gauge.js, same class the hero
     * dial on index.html uses) inside a just-inserted chat message and sweep
     * it to the looked-up alert's score. */
    wireGauge(msgEl, alert) {
      if (typeof TrustSphereGauge === 'undefined') return;
      const root = msgEl.querySelector('[data-gauge]');
      if (!root) return;
      const gauge = new TrustSphereGauge(root);
      gauge.score({
        score: Math.round((alert.exposure ?? 0) * 100),
        tier: alert.ALERT_PRIORITY || 'MEDIUM',
      });
    }

    /**
     * Builds the mini ownership graph inside a just-inserted chat card and
     * wires hover/focus/tap interactivity: touching a node highlights the
     * edges and neighbours it's actually connected to, and swaps the caption
     * for a plain-English sentence built straight from that node's edges
     * (caseData.graph.edges), not a separate hardcoded blurb per node -- so
     * this stays correct if the mock case data changes. No scroll trigger,
     * no ScrollTrigger dependency: the graph is fully drawn immediately,
     * matching the "no scrollable UI" ask for a chat bubble.
     */
    wireCaseGraph(msgEl, caseData) {
      const root = msgEl.querySelector('[data-chat-graph]');
      const svg = msgEl.querySelector('[data-chat-graph-svg]');
      const captionEl = msgEl.querySelector('[data-chat-graph-caption]');
      if (!root || !svg) return;

      const svgNS = 'http://www.w3.org/2000/svg';
      const nodes = caseData.graph.nodes;
      const edges = caseData.graph.edges;
      const nodesById = Object.fromEntries(nodes.map((n) => [n.id, n]));
      const plain = (id) => nodesById[id].label.replace(/\n/g, ' ');

      const edgeEls = edges.map((e) => {
        const from = CASE_GRAPH_LAYOUT[e.from];
        const to = CASE_GRAPH_LAYOUT[e.to];
        const line = document.createElementNS(svgNS, 'line');
        line.setAttribute('x1', from.x);
        line.setAttribute('y1', from.y);
        line.setAttribute('x2', to.x);
        line.setAttribute('y2', to.y);
        line.setAttribute('class', e.to === 'ubo' ? 'chat-graph__edge chat-graph__edge--ubo' : 'chat-graph__edge');
        svg.appendChild(line);
        return { el: line, from: e.from, to: e.to, label: e.label };
      });

      const nodeEls = nodes.map((n) => {
        const pos = CASE_GRAPH_LAYOUT[n.id];
        const r = CASE_GRAPH_RADIUS[n.kind] || CASE_GRAPH_RADIUS.shell;
        const g = document.createElementNS(svgNS, 'g');
        g.setAttribute('class', `chat-graph__node chat-graph__node--${n.kind}`);
        g.setAttribute('tabindex', '0');
        g.setAttribute('role', 'button');
        g.setAttribute('aria-label', plain(n.id));
        const circle = document.createElementNS(svgNS, 'circle');
        circle.setAttribute('cx', pos.x);
        circle.setAttribute('cy', pos.y);
        circle.setAttribute('r', r);
        g.appendChild(circle);
        svg.appendChild(g);
        return { id: n.id, el: g };
      });

      const setActive = (id) => {
        nodeEls.forEach(({ id: nid, el }) => el.classList.toggle('is-active', nid === id));
        edgeEls.forEach(({ el, from, to }) =>
          el.classList.toggle('is-active', id != null && (from === id || to === id)));
        if (!captionEl) return;
        if (id == null) {
          captionEl.textContent = CASE_GRAPH_HINT;
          return;
        }
        const touching = edgeEls.filter((e) => e.from === id || e.to === id);
        captionEl.textContent = touching.length
          ? touching.map((e) => `${plain(e.from)} ${e.label} ${plain(e.to)}.`).join(' ')
          : plain(id);
      };

      nodeEls.forEach(({ id, el }) => {
        el.addEventListener('mouseenter', () => setActive(id));
        el.addEventListener('mouseleave', () => setActive(null));
        el.addEventListener('focus', () => setActive(id));
        el.addEventListener('blur', () => setActive(null));
        el.addEventListener('click', () => setActive(id));
      });
    }

    async handleSend(rawText) {
      const text = (rawText || '').trim();
      if (!text || this.submitBtn.disabled) return;

      this.appendUser(text);
      this.input.value = '';
      this.submitBtn.disabled = true;
      this.input.disabled = true;

      const typingMsg = this.appendTyping();
      // A live network call already has real latency; a local answer gets a
      // short floor so it doesn't feel like the input was ignored.
      const minDelay = reduceMotion ? Promise.resolve() : new Promise((r) => setTimeout(r, 450));
      const caseNumber = extractCaseNumber(text);

      let html, alertForGauge = null, caseForGraph = null;
      try {
        if (caseNumber) {
          const [lookup] = await Promise.all([this.resolveCaseLookup(caseNumber), minDelay]);
          html = lookup.html;
          alertForGauge = lookup.alert;
          caseForGraph = lookup.graphCase;
        } else {
          const [result] = await Promise.all([this.resolveAnswer(text), minDelay]);
          html = result.html;
          caseForGraph = result.caseData;
        }
      } catch (err) {
        html = '<p>Something went wrong answering that — please try again.</p>';
      }

      typingMsg.remove();
      const msg = this.appendAssistant(html);
      if (alertForGauge) this.wireGauge(msg, alertForGauge);
      if (caseForGraph && caseForGraph.graph) this.wireCaseGraph(msg, caseForGraph);
      this.submitBtn.disabled = false;
      this.input.disabled = false;
      this.input.focus();
    }
  }

  window.TrustSphereQueryConsole = TrustSphereQueryConsole;
  new TrustSphereQueryConsole(document.querySelector('[data-console]'));
})();
