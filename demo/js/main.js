/**
 * Hero wiring: populate the transaction card from mock data, and on
 * "Score this transaction" run the gauge sweep + rationale reveal.
 *
 * REPLACE-WITH-API: `runScore()` currently reads from TRUSTSPHERE_CASES.
 * The real integration replaces `getCase(id)` with an awaited call to
 * POST /score against the FastAPI service and uses its JSON response
 * in place of the case object.
 */
(function () {
  gsap.registerPlugin(ScrollTrigger);

  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  const fmtUsd = (n) =>
    new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 }).format(n);

  function getCase(id) {
    return TRUSTSPHERE_CASES.find((c) => c.id === id) || TRUSTSPHERE_CASES[0];
  }

  function populateTxnCard(caseData) {
    const card = document.querySelector('[data-txn-card]');
    const t = caseData.transaction;
    card.querySelector('[data-field="id"]').textContent = t.id;
    card.querySelector('[data-field="corridor"]').textContent = caseData.corridor;
    card.querySelector('[data-field="amount"]').textContent = fmtUsd(t.amountUsd);
    card.querySelector('[data-field="originator"]').textContent = `${t.originator} (${t.originatingCountry})`;
    card.querySelector('[data-field="beneficiary"]').textContent = `${t.beneficiary} (${t.destinationCountry})`;
    card.querySelector('[data-field="entityType"]').textContent = t.entityType;
  }

  function populateFactors(caseData) {
    const list = document.querySelector('[data-factor-list]');
    list.innerHTML = '';
    const top = [...caseData.factors].sort((a, b) => b.contribution - a.contribution).slice(0, 3);
    top.forEach((f) => {
      const li = document.createElement('li');
      li.innerHTML = `
        <span class="factor-name">${f.label}</span>
        <span class="factor-weight">w ${f.weight.toFixed(2)} · ${Math.round(f.contribution * 100)}</span>
        <span class="factor-detail">${f.detail}</span>
      `;
      list.appendChild(li);
    });
  }

  function revealRationale(caseData) {
    const panel = document.querySelector('[data-rationale]');
    const linesEl = document.querySelector('[data-rationale-lines]');
    linesEl.innerHTML = '';
    caseData.rationale.forEach((line) => {
      const li = document.createElement('li');
      li.textContent = line;
      linesEl.appendChild(li);
    });
    populateFactors(caseData);
    panel.hidden = false;

    // Unhiding this panel changes the hero's height, which shifts every
    // section below it (pipeline, graph, severity matrix) down the page.
    // Their ScrollTriggers were positioned against the shorter pre-reveal
    // layout, so without a refresh they'd fire at the wrong scroll offset
    // for the rest of the session.
    ScrollTrigger.refresh();

    if (reduceMotion) {
      gsap.set(linesEl.children, { opacity: 1, y: 0 });
      gsap.set('[data-factor-list] li', { opacity: 1, y: 0 });
      return;
    }

    gsap.fromTo(
      linesEl.children,
      { opacity: 0, y: 10 },
      { opacity: 1, y: 0, duration: 0.5, ease: 'power2.out', stagger: 0.16 }
    );
    gsap.fromTo(
      '[data-factor-list] li',
      { opacity: 0, y: 8 },
      { opacity: 1, y: 0, duration: 0.4, ease: 'power2.out', stagger: 0.1, delay: 0.3 }
    );
  }

  function runScore(caseData, button) {
    if (button) {
      button.disabled = true;
      button.textContent = 'Scoring…';
    }
    const panel = document.querySelector('[data-rationale]');
    panel.hidden = true;

    const tl = gauge.score(caseData);
    revealRationale(caseData);

    const finish = () => {
      if (button) {
        button.disabled = false;
        button.textContent = 'Score this transaction';
      }
    };

    if (reduceMotion) {
      finish();
    } else {
      tl.eventCallback('onComplete', finish);
    }
  }

  const gaugeRoot = document.querySelector('[data-gauge]');
  const gauge = new TrustSphereGauge(gaugeRoot);

  let activeCaseId = TRUSTSPHERE_DEFAULT_CASE_ID;
  populateTxnCard(getCase(activeCaseId));

  document.querySelector('[data-score-btn]').addEventListener('click', (e) => {
    runScore(getCase(activeCaseId), e.currentTarget);
  });

  const graphRoot = document.querySelector('.graph-section');
  const graph = new TrustSphereGraph(graphRoot, getCase('shell'));
  graph.animate();

  const pipelineRoot = document.querySelector('[data-pipeline]');
  const pipeline = new TrustSpherePipeline(pipelineRoot);
  pipeline.animate();

  const severityRoot = document.querySelector('.severity-section');
  const severity = new TrustSphereSeverityMatrix(severityRoot, getCase('shell'));
  severity.animate();
})();
