/**
 * Shared motion for the console, built on anime.js.
 *
 * The rule this file exists to enforce: motion has to carry meaning. A list
 * whose items arrive in sequence tells you they are a set and roughly how
 * many. A number that travels to its new value tells you it changed rather
 * than that you misread it. A check mark that draws tells you something
 * finished just now, as opposed to having been finished all along.
 *
 * Anything that does not say one of those things is decoration, and decoration
 * on an operations console is noise on a screen somebody is reading during an
 * incident.
 *
 * Two hard rules, both enforced here rather than remembered per component:
 *
 *   Nobody who has asked their system for less movement gets any. Every helper
 *   checks prefers-reduced-motion and jumps straight to the final state; it
 *   never simply skips the animation and leaves an element at opacity 0.
 *
 *   Motion never gates interaction. Elements are visible and clickable first;
 *   the animation adjusts how they got there.
 */

import { animate, stagger, utils, type AnimationParams } from "animejs";
import { useEffect, useRef } from "react";

/** The console's motion vocabulary, matching styles/theme.ts. */
export const DUR = {
  fast: 150,
  base: 220,
  slow: 320,
} as const;

export const EASE = {
  /** Default for a thing changing in place. Decelerates into its value. */
  standard: "cubicBezier(0.2, 0, 0, 1)",
  /** For something travelling a visible distance. Overshoots and settles. */
  emphasis: "cubicBezier(0.34, 1.56, 0.64, 1)",
  /** Leaving is faster than arriving. */
  exit: "cubicBezier(0.4, 0, 1, 1)",
} as const;

export function prefersReducedMotion(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  try {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch {
    return false;
  }
}

/**
 * Run an animation, or apply its end state immediately.
 *
 * The fallback is the important half. Skipping an animation that was going to
 * fade something in leaves it invisible, which is how "reduced motion" turns
 * into "blank page".
 */
function run(targets: Parameters<typeof animate>[0], params: AnimationParams) {
  if (prefersReducedMotion()) {
    const end: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(params)) {
      if (["duration", "delay", "ease", "easing", "loop", "onComplete", "autoplay"].includes(key)) {
        continue;
      }
      end[key] = Array.isArray(value) ? value[value.length - 1] : value;
    }
    try {
      utils.set(targets, end as never);
    } catch {
      /* a target that has gone away is not an error worth surfacing */
    }
    params.onComplete?.(undefined as never);
    return null;
  }
  return animate(targets, params);
}

/**
 * Children of `ref` arrive in sequence.
 *
 * `key` restarts it: pass something that changes when the list is genuinely a
 * different list, and nothing when it is the same list re-rendered. Animating
 * on every render is how a page ends up flickering while somebody types.
 */
export function useStaggerIn(
  ref: React.RefObject<HTMLElement | null>,
  key: unknown = null,
  opts: { distance?: number; step?: number; max?: number } = {},
) {
  const { distance = 8, step = 40, max = 8 } = opts;
  useEffect(() => {
    const host = ref.current;
    if (!host) return;
    // Capped rather than staggered to the end: past about eight items the last
    // one is waiting on decoration, and an operator scrolling down a long list
    // should not find it still arriving.
    const kids = (Array.from(host.children) as HTMLElement[]).slice(0, max + 1);
    if (!kids.length) return;
    run(kids, {
      opacity: [0, 1],
      translateY: [distance, 0],
      duration: DUR.slow,
      // Capped: past about eight items the last one is waiting on decoration,
      // and an operator scrolling to the bottom of a long list should not
      // find it still arriving.
      delay: stagger(step, { from: 0 }),
      ease: EASE.standard,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
}

/**
 * A number travels to its new value.
 *
 * Only worth it when the number is being watched for change. On a value that
 * is simply displayed, it is a distraction.
 */
export function useCountUp(
  ref: React.RefObject<HTMLElement | null>,
  value: number,
  format: (n: number) => string = (n) => String(Math.round(n)),
) {
  const previous = useRef<number | null>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const from = previous.current;
    previous.current = value;
    if (from === null || from === value || !Number.isFinite(value)) {
      el.textContent = format(value);
      return;
    }
    const state = { n: from };
    run(state, {
      n: value,
      duration: DUR.slow,
      ease: EASE.standard,
      onUpdate: () => {
        el.textContent = format(state.n);
      },
      onComplete: () => {
        el.textContent = format(value);
      },
    } as AnimationParams);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);
}

/**
 * Draw an SVG path once, for a mark that means "this just completed".
 *
 * Pointless on something that was already true when the page loaded, which is
 * why it takes an explicit trigger rather than running on mount.
 */
export function drawPath(path: SVGPathElement | null, on: boolean) {
  if (!path || !on) return;
  let length = 0;
  try {
    length = path.getTotalLength();
  } catch {
    return;
  }
  if (!length) return;
  path.style.strokeDasharray = String(length);
  path.style.strokeDashoffset = String(length);
  run(path, {
    strokeDashoffset: [length, 0],
    duration: DUR.base,
    ease: EASE.standard,
  } as AnimationParams);
}

/**
 * A short, non-repeating attention pulse.
 *
 * For the moment a value the operator is waiting on actually changes. Never on
 * a loop: a console that pulses continuously is a console people stop looking
 * at.
 */
export function pulse(el: HTMLElement | null) {
  if (!el) return;
  run(el, {
    scale: [1, 1.04, 1],
    duration: DUR.base * 2,
    ease: EASE.emphasis,
  } as AnimationParams);
}
