/** Text fitting for places where CSS cannot clip.
 *
 * SVG has no overflow: a label wider than the shape it sits in simply draws
 * over whatever is next to it. Every such label is budgeted against a fixed
 * pixel width, so the helper that shortens it has to respect the budget it is
 * given - the earlier version returned `max - 1` characters plus "...", which
 * is `max + 2`, and every one of those budgets was quietly two characters
 * short.
 */

/** At most `max` characters, ellipsis included. */
export function truncate(text: string, max: number): string {
  const s = text ?? "";
  if (max <= 0) return "";
  if (max <= 3) return s.slice(0, max);
  return s.length > max ? `${s.slice(0, max - 3)}...` : s;
}

/** How many characters of `px` width fit, for a given average glyph width.
 *
 * Approximate by design: it exists so a caller states the width it actually
 * has instead of hard-coding a character count that nobody can check against
 * the shape it has to fit inside.
 */
export function charsThatFit(px: number, avgCharPx: number): number {
  if (avgCharPx <= 0) return 0;
  return Math.max(0, Math.floor(px / avgCharPx));
}
