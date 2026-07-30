/**
 * Severity matrix reveal: the likelihood x severity lookup table from
 * config/severity_matrix.yaml assembles cell by cell on scroll, landing on
 * the case's own cell last (high likelihood x high severity for the
 * shell-company case -> Critical).
 */
(function () {
  class TrustSphereSeverityMatrix {
    constructor(root, caseData) {
      this.root = root;
      this.caseData = caseData;
      this.cells = Array.from(root.querySelectorAll('[data-cell]'));
      this.highlightCell = root.querySelector('[data-severity-highlight]');
      this.caption = root.querySelector('[data-severity-caption]');
      this.reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

      if (!this.reduceMotion) {
        gsap.set(this.cells, { opacity: 0, scale: 0.85 });
        gsap.set(this.caption, { opacity: 0, y: 8 });
      }
    }

    animate() {
      if (this.reduceMotion) {
        this.highlightCell.classList.add('is-final');
        return;
      }

      const stage = this.root.querySelector('[data-severity-matrix]');
      const otherCells = this.cells.filter((c) => c !== this.highlightCell);

      const tl = gsap.timeline({
        scrollTrigger: {
          trigger: stage,
          start: 'top 75%',
          toggleActions: 'play none none reverse',
        },
      });

      tl.to(otherCells, {
        opacity: 1,
        scale: 1,
        duration: 0.4,
        stagger: { each: 0.06, from: 'start' },
        ease: 'power2.out',
      })
        .to(this.highlightCell, {
          opacity: 1,
          scale: 1,
          duration: 0.5,
          ease: 'back.out(2.2)',
        }, '+=0.1')
        .call(() => this.highlightCell.classList.add('is-final'))
        .fromTo(this.caption, { opacity: 0, y: 8 }, { opacity: 1, y: 0, duration: 0.4, ease: 'power2.out' }, '<');
    }
  }

  window.TrustSphereSeverityMatrix = TrustSphereSeverityMatrix;
})();
