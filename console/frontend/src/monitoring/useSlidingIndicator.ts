import { useLayoutEffect, useRef, useState } from "react";

/**
 * Track the position and width of the selected button in a segmented control,
 * so one block can slide between the options instead of each button turning
 * its own background on and off.
 *
 * Two segmented controls in this console need it: the time range on the
 * Monitoring tab and the period on the Reports tab. The measurement is the
 * only fiddly part, so it lives here once rather than being written twice and
 * drifting.
 *
 * Measured rather than derived from an index, because the labels have very
 * different widths ("Now (live)" against "1 hour") and because fonts loading,
 * a resized window or a wrapped toolbar all move the buttons afterwards. A
 * hand-computed offset would be visibly wrong in exactly those cases.
 *
 * Movement itself is CSS on the indicator element. Anyone who has asked their
 * system for reduced motion gets none of it: the rule in global.css collapses
 * every transition in the app, including this one.
 */
export type Indicator = { x: number; w: number };

export function useSlidingIndicator<Group extends HTMLElement, Item extends HTMLElement>(
  /** Anything that changes which item is selected, or how wide the items are. */
  deps: readonly unknown[],
) {
  const groupRef = useRef<Group | null>(null);
  const activeRef = useRef<Item | null>(null);
  const [indicator, setIndicator] = useState<Indicator>({ x: 0, w: 0 });

  useLayoutEffect(() => {
    const measure = () => {
      const group = groupRef.current;
      const active = activeRef.current;
      if (!group || !active) return;
      setIndicator({ x: active.offsetLeft, w: active.offsetWidth });
    };
    // Before paint, so the block is never drawn in the wrong place first and
    // then seen to jump to the right one.
    measure();
    // ResizeObserver is not defined in every environment this bundle is
    // parsed in, so fall back rather than throwing at module scope.
    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", measure);
      return () => window.removeEventListener("resize", measure);
    }
    const observer = new ResizeObserver(measure);
    if (groupRef.current) observer.observe(groupRef.current);
    return () => observer.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  /** True once a real measurement exists, so the block can be hidden until then. */
  const ready = indicator.w > 0;
  return { groupRef, activeRef, indicator, ready };
}
