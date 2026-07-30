/**
 * Entity-relationship graph reveal: a field of scattered, unlabeled points
 * (standing in for raw, unlinked transaction/company records) resolves into
 * a structured beneficial-ownership graph as the user scrolls — the same
 * "unstructured -> structured" scroll technique used in the Hexabel
 * HeroDataField component, adapted from a resolving trend line to a
 * resolving node-link graph.
 *
 * Driven by the shell-company case's `graph` data in data.js. Node layout
 * (final positions) is presentation-only and lives here, not in data.js.
 */
(function () {
  const VIEW_W = 760;
  const VIEW_H = 460;
  const NOISE_COUNT = 34;

  const LAYOUT = {
    originator: { x: 110, y: 90 },
    beneficiary: { x: 300, y: 150 },
    shell2: { x: 430, y: 88 },
    shell3: { x: 620, y: 150 },
    shell4: { x: 650, y: 300 },
    ubo: { x: 400, y: 385 },
  };

  const RADIUS = { originator: 9, shell: 9, ubo: 13 };

  function mulberry32(seed) {
    return function () {
      seed |= 0;
      seed = (seed + 0x6d2b79f5) | 0;
      let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function scatterPoint(rand, margin) {
    return {
      x: margin + rand() * (VIEW_W - margin * 2),
      y: margin + rand() * (VIEW_H - margin * 2),
    };
  }

  class TrustSphereGraph {
    constructor(root, caseData) {
      this.root = root;
      this.svg = root.querySelector('[data-graph-svg]');
      this.caption = root.querySelector('[data-graph-caption]');
      this.caseData = caseData;
      this.reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      this._build();
    }

    _build() {
      const svgNS = 'http://www.w3.org/2000/svg';
      const rand = mulberry32(7301);
      const nodes = this.caseData.graph.nodes;
      const edges = this.caseData.graph.edges;

      const noiseGroup = document.createElementNS(svgNS, 'g');
      noiseGroup.setAttribute('data-graph-noise', '');
      const noiseEls = [];
      for (let i = 0; i < NOISE_COUNT; i++) {
        const p = scatterPoint(rand, 16);
        const c = document.createElementNS(svgNS, 'circle');
        c.setAttribute('cx', p.x.toFixed(1));
        c.setAttribute('cy', p.y.toFixed(1));
        c.setAttribute('r', (1.4 + rand() * 1.8).toFixed(2));
        c.setAttribute('class', 'graph-noise-dot');
        noiseGroup.appendChild(c);
        noiseEls.push(c);
      }

      const edgeGroup = document.createElementNS(svgNS, 'g');
      edgeGroup.setAttribute('data-graph-edges', '');
      const edgeEls = edges.map((e) => {
        const from = LAYOUT[e.from];
        const to = LAYOUT[e.to];
        const line = document.createElementNS(svgNS, 'line');
        line.setAttribute('x1', from.x);
        line.setAttribute('y1', from.y);
        line.setAttribute('x2', to.x);
        line.setAttribute('y2', to.y);
        const isUboEdge = e.to === 'ubo';
        line.setAttribute('class', isUboEdge ? 'graph-edge graph-edge--ubo' : 'graph-edge');
        const length = Math.hypot(to.x - from.x, to.y - from.y);
        line.style.strokeDasharray = String(length);
        line.style.strokeDashoffset = String(length);
        edgeGroup.appendChild(line);
        return line;
      });

      const nodeGroup = document.createElementNS(svgNS, 'g');
      nodeGroup.setAttribute('data-graph-nodes', '');
      const nodeEls = [];
      const labelEls = [];
      nodes.forEach((n) => {
        const start = scatterPoint(rand, 40);
        const final = LAYOUT[n.id];
        const r = RADIUS[n.kind] || RADIUS.shell;

        const g = document.createElementNS(svgNS, 'g');
        g.setAttribute('class', `graph-node graph-node--${n.kind}`);

        const circle = document.createElementNS(svgNS, 'circle');
        circle.setAttribute('cx', start.x.toFixed(1));
        circle.setAttribute('cy', start.y.toFixed(1));
        circle.setAttribute('r', r);
        g.appendChild(circle);

        const label = document.createElementNS(svgNS, 'text');
        label.setAttribute('class', 'graph-node__label');
        label.setAttribute('text-anchor', 'middle');
        label.setAttribute('x', final.x);
        label.setAttribute('y', final.y + r + 16);
        n.label.split('\n').forEach((line, i) => {
          const tspan = document.createElementNS(svgNS, 'tspan');
          tspan.setAttribute('x', final.x);
          tspan.setAttribute('dy', i === 0 ? '0' : '1.15em');
          tspan.textContent = line;
          label.appendChild(tspan);
        });

        nodeGroup.appendChild(g);
        this.svg.appendChild(label);
        nodeEls.push({ el: circle, start, final });
        labelEls.push(label);
      });

      this.svg.appendChild(noiseGroup);
      this.svg.appendChild(edgeGroup);
      this.svg.appendChild(nodeGroup);

      this._noiseEls = noiseEls;
      this._edgeEls = edgeEls;
      this._nodeEls = nodeEls;
      this._labelEls = labelEls;
    }

    /** Wires the scroll-driven resolve animation. Call once, after building. */
    animate() {
      const { _noiseEls: noiseEls, _edgeEls: edgeEls, _nodeEls: nodeEls, _labelEls: labelEls } = this;

      if (this.reduceMotion) {
        noiseEls.forEach((el) => el.style.setProperty('opacity', '0'));
        nodeEls.forEach(({ el, final }) => {
          el.setAttribute('cx', final.x);
          el.setAttribute('cy', final.y);
        });
        edgeEls.forEach((el) => { el.style.strokeDashoffset = '0'; });
        labelEls.forEach((el) => el.style.setProperty('opacity', '1'));
        return;
      }

      gsap.set(labelEls, { opacity: 0 });

      const stage = this.root.querySelector('[data-graph]');

      // No pin, no held/dead scroll distance: the animation is scrubbed
      // directly against the stage's own entrance, the same technique as the
      // Hexabel hero data-field. Viewport-relative end markers are a trap
      // here: `end: 'top top'` is exactly one viewport-height of scroll no
      // matter the screen size, which a single spacebar/Page Down press also
      // covers -- so on a tall monitor the whole reveal completes in one
      // keystroke instead of scrubbing. `end: 'bottom top'` fixes that but
      // then depends on scrolling into whatever follows the stage, which can
      // run out of room on a short page. A fixed pixel distance sidesteps
      // both: the margin needed past it doesn't grow with viewport height
      // (verified against the page's actual length), and 950px comfortably
      // exceeds a single scroll/Page Down step without requiring extra
      // reserved space after the footer.
      const tl = gsap.timeline({
        scrollTrigger: {
          trigger: stage,
          start: 'top bottom',
          end: '+=950',
          scrub: 0.3,
        },
      });

      // Motion is confined to the middle of the trigger range: held scattered
      // for the first 15% (so nothing moves while the stage is still only a
      // sliver at the bottom edge of the viewport), all real movement inside
      // the middle 70%, then held resolved for the last 15% (buffer against
      // scrub lag). `at()`/`dur()` map a 0-1 "active window" onto that
      // middle slice so the choreography below reads the same way it would
      // without the lead-in/settle padding.
      const LEAD_IN = 0.15;
      const SETTLE = 0.85;
      const ACTIVE = SETTLE - LEAD_IN;
      const at = (f) => LEAD_IN + f * ACTIVE;
      const dur = (d) => d * ACTIVE;

      tl.to(noiseEls, { opacity: 0, duration: dur(0.4), stagger: 0.006, ease: 'none' }, at(0));

      nodeEls.forEach(({ el, final }, i) => {
        tl.to(el, {
          attr: { cx: final.x, cy: final.y },
          duration: dur(0.55),
          ease: 'power2.inOut',
        }, at(0.05 + i * 0.025));
      });

      tl.to(edgeEls.map((e) => e), {
        strokeDashoffset: 0,
        duration: dur(0.2),
        stagger: dur(0.04),
        ease: 'none',
      }, at(0.55));

      tl.to(labelEls, { opacity: 1, duration: dur(0.15), stagger: dur(0.03), ease: 'none' }, at(0.68));

      // Zero-effect filler: extends the timeline's total duration to 1.0 so
      // the settled state actually holds for the reserved final 15%.
      tl.to({}, { duration: 1 - SETTLE }, SETTLE);
    }
  }

  window.TrustSphereGraph = TrustSphereGraph;
})();
