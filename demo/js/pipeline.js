/**
 * Pipeline schematic: the eight phases (Phase 1 ingestion -> Phase 8 human
 * review) light up in order as the reader scrolls past them, with a rail
 * behind the markers filling in at the same pace as scroll position.
 *
 * The rail fill is a genuine scrubbed animation (not pinned) run against the
 * list's own natural height, so it doesn't fight native scroll the way a
 * pinned scroll-jack section would. Each step's light-up is a separate,
 * independently reversible ScrollTrigger rather than one shared timeline, so
 * scrolling back up un-lights steps in the right order for free.
 */
(function () {
  class TrustSpherePipeline {
    constructor(root) {
      this.root = root;
      this.list = root.querySelector('[data-pipeline-list]');
      this.railFill = root.querySelector('[data-pipeline-rail-fill]');
      this.steps = Array.from(root.querySelectorAll('[data-pipeline-step]'));
      this.reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    }

    animate() {
      if (this.reduceMotion) {
        this.steps.forEach((step) => step.classList.add('is-lit'));
        gsap.set(this.railFill, { scaleY: 1 });
        return;
      }

      gsap.to(this.railFill, {
        scaleY: 1,
        ease: 'none',
        scrollTrigger: {
          trigger: this.list,
          start: 'top 70%',
          end: 'bottom 55%',
          scrub: 0.3,
        },
      });

      this.steps.forEach((step) => {
        ScrollTrigger.create({
          trigger: step,
          start: 'top 80%',
          onEnter: () => step.classList.add('is-lit'),
          onLeaveBack: () => step.classList.remove('is-lit'),
        });
      });
    }
  }

  window.TrustSpherePipeline = TrustSpherePipeline;
})();
