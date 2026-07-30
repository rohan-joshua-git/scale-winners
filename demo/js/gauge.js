/**
 * TrustSphereGauge — an SVG instrument-panel dial.
 *
 * Renders a 270-degree bezel with tick marks, a track arc, a violet progress
 * arc, a platinum needle, and a Plex Mono numeric readout. On `.score()` it
 * sweeps the needle/arc into position with an overshoot-and-settle ease,
 * counts the number up, then washes the tier color into the bezel glow.
 */
class TrustSphereGauge {
  static START_ANGLE = -135;
  static END_ANGLE = 135;

  constructor(root) {
    this.root = root;
    this.svg = root.querySelector('[data-gauge-svg]');
    this.cx = 150;
    this.cy = 150;
    this.r = 118;
    this.reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    this._buildStatic();
    this.scoreEl = root.querySelector('[data-gauge-score]');
    this.tierEl = root.querySelector('[data-gauge-tier]');
    this.needle = root.querySelector('[data-gauge-needle]');
    this.progressArc = root.querySelector('[data-gauge-progress]');
    this.housing = root.querySelector('[data-gauge-housing]');
    this._current = { score: 0 };
    this._setAngle(TrustSphereGauge.START_ANGLE);
    this._setProgress(0);
  }

  static pointOnCircle(cx, cy, r, angleDeg) {
    const rad = (angleDeg * Math.PI) / 180;
    return { x: cx + r * Math.sin(rad), y: cy - r * Math.cos(rad) };
  }

  static arcPath(cx, cy, r, a0, a1) {
    const p0 = TrustSphereGauge.pointOnCircle(cx, cy, r, a0);
    const p1 = TrustSphereGauge.pointOnCircle(cx, cy, r, a1);
    const largeArc = a1 - a0 > 180 ? 1 : 0;
    return `M ${p0.x.toFixed(2)} ${p0.y.toFixed(2)} A ${r} ${r} 0 ${largeArc} 1 ${p1.x.toFixed(2)} ${p1.y.toFixed(2)}`;
  }

  _buildStatic() {
    const { cx, cy, r } = this;
    const svgNS = 'http://www.w3.org/2000/svg';
    const ticksGroup = this.svg.querySelector('[data-gauge-ticks]');
    ticksGroup.innerHTML = '';

    const tickCount = 27; // every 10 degrees across the 270-degree sweep
    for (let i = 0; i <= tickCount; i++) {
      const angle = TrustSphereGauge.START_ANGLE + (i / tickCount) * (TrustSphereGauge.END_ANGLE - TrustSphereGauge.START_ANGLE);
      const isMajor = i % 5 === 0;
      const outer = TrustSphereGauge.pointOnCircle(cx, cy, r, angle);
      const inner = TrustSphereGauge.pointOnCircle(cx, cy, r - (isMajor ? 14 : 7), angle);
      const line = document.createElementNS(svgNS, 'line');
      line.setAttribute('x1', outer.x.toFixed(2));
      line.setAttribute('y1', outer.y.toFixed(2));
      line.setAttribute('x2', inner.x.toFixed(2));
      line.setAttribute('y2', inner.y.toFixed(2));
      line.setAttribute('class', isMajor ? 'gauge-tick gauge-tick--major' : 'gauge-tick');
      ticksGroup.appendChild(line);
    }

    const track = this.svg.querySelector('[data-gauge-track]');
    track.setAttribute('d', TrustSphereGauge.arcPath(cx, cy, r, TrustSphereGauge.START_ANGLE, TrustSphereGauge.END_ANGLE));
  }

  _setAngle(angle) {
    this.needle.setAttribute('transform', `rotate(${angle} ${this.cx} ${this.cy})`);
  }

  _setProgress(score) {
    const angle = TrustSphereGauge.START_ANGLE + (score / 100) * (TrustSphereGauge.END_ANGLE - TrustSphereGauge.START_ANGLE);
    this.progressArc.setAttribute('d', TrustSphereGauge.arcPath(this.cx, this.cy, this.r, TrustSphereGauge.START_ANGLE, angle));
  }

  /** Animate the gauge to a case's score/tier. Returns the GSAP timeline. */
  score(caseData) {
    const targetAngle = TrustSphereGauge.START_ANGLE + (caseData.score / 100) * (TrustSphereGauge.END_ANGLE - TrustSphereGauge.START_ANGLE);
    const tierClass = `tier-${caseData.tier.toLowerCase()}`;

    this.root.classList.remove('tier-low', 'tier-medium', 'tier-high', 'tier-critical', 'is-landed');
    this.tierEl.textContent = '';
    this.scoreEl.textContent = '0';

    const proxy = { score: 0, angle: TrustSphereGauge.START_ANGLE };
    const tl = gsap.timeline();

    if (this.reduceMotion) {
      this._setAngle(targetAngle);
      this._setProgress(caseData.score);
      this.scoreEl.textContent = String(caseData.score);
      this.root.classList.add(tierClass, 'is-landed');
      this.tierEl.textContent = caseData.tier;
      return tl;
    }

    tl.to(proxy, {
      score: caseData.score,
      angle: targetAngle,
      duration: 1.5,
      ease: 'back.out(1.15)',
      onUpdate: () => {
        this._setAngle(proxy.angle);
        this._setProgress(proxy.score);
        this.scoreEl.textContent = String(Math.round(proxy.score));
      },
    }).call(() => {
      this.root.classList.add(tierClass, 'is-landed');
    }, null, '-=0.15')
      .fromTo(this.tierEl, { opacity: 0, y: 6 }, { opacity: 1, y: 0, duration: 0.5, ease: 'power2.out', onStart: () => { this.tierEl.textContent = caseData.tier; } }, '-=0.2');

    return tl;
  }
}
