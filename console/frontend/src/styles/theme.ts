/** Shared Emotion theme tokens for the console (light UI only).
 *
 * Paper, charcoal and oxide. The console is a tool used in a server room, not
 * a SaaS dashboard: warm paper rather than cold white, hairline rules rather
 * than floating cards, ink rather than a blue accent, and one loud colour
 * (oxide) reserved for the action that commits something.
 *
 * The token NAMES are unchanged on purpose. Roughly 460 call sites across the
 * app already reference them, and remapping the values carries the whole look
 * without touching a single component's logic. `surface` keeps its 50 to 900
 * shape and is simply re-cast onto the warm scale, so an existing
 * `theme.surface[100]` background lands on recessed paper instead of cold
 * grey.
 */

/** Oxide. The primary commit and the destructive action are the same family:
 * on this palette a red-brown IS the loud colour, and having two of them would
 * make neither mean anything. */
export const primary = {
  50: "#FBF0EE",
  100: "#F5DCD9",
  200: "#E8B5AF",
  300: "#DA8B82",
  400: "#C85448",
  500: "#B42318",
  600: "#A11F15",
  700: "#8F1C13",
  800: "#6E150F",
  900: "#4A0E0A",
} as const;

/** Warm neutrals, light to dark. 50 is raised paper, 900 is ink. */
export const surface = {
  50: "#FBFAF7",
  100: "#ECE8DF",
  200: "#D4D0C8",
  300: "#C4BFB4",
  400: "#9B968D",
  500: "#6B6760",
  600: "#514D46",
  700: "#3A3733",
  800: "#2C2A26",
  900: "#1A1916",
} as const;

export const theme = {
  primary,
  surface,

  /* Severity, kept for the alert and task surfaces that already use them. */
  critical: primary[500],
  high: "#9A6B12",
  medium: "#9A6B12",
  low: "#2F6F4E",
  completed: "#2F6F4E",
  blueprint: surface[500],

  /* Page and panels. */
  paper: "#F4F1EA",
  bg: "#F4F1EA",
  bgElev: surface[50],
  bgPanel: surface[100],
  rail: surface[800],
  railRaised: surface[700],
  sidebar: surface[800],

  ink: surface[900],
  muted: surface[500],
  line: surface[200],

  /* `accent` is the QUIET emphasis: the active tab, the current wizard step,
   * the focus ring, a chart trace. On paper that is ink. It is deliberately
   * NOT oxide, or every tab and every graph line would read as an alarm. */
  accent: surface[900],
  accentHover: surface[700],
  accentSoft: "rgba(26, 25, 22, 0.06)",

  /* `oxide` is the LOUD emphasis: the one button on a screen that commits
   * something, and anything irreversible. */
  oxide: primary[500],
  oxideHover: primary[700],
  oxideSoft: "rgba(180, 35, 24, 0.08)",

  danger: primary[500],
  dangerSoft: "rgba(180, 35, 24, 0.08)",
  warn: "#9A6B12",
  warnSoft: "rgba(154, 107, 18, 0.10)",
  ok: "#2F6F4E",
  okSoft: "rgba(47, 111, 78, 0.10)",

  /* Severity as it reads ON THE CHARCOAL RAIL. Oxide and ochre are tuned for
   * paper and go muddy on charcoal, so the rail gets lighter tints of the same
   * two families rather than a different palette. */
  oxideOnRail: primary[200],
  warnOnRail: "#D7B26A",

  /* KIN blue survives as a brand mark only: a small square beside the
   * lockup. Never a button, never an active nav fill. */
  brand: "#0061FF",

  radius: {
    sm: "2px",
    md: "3px",
    lg: "4px",
    xl: "4px",
    "2xl": "4px",
    /* There are no pills on paper. Kept as a key so call sites still compile. */
    full: "2px",
  },
  /* Almost nothing. Separation comes from rules and left borders. The larger
   * steps exist only so an overlay reads as above the page rather than
   * printed on it. */
  shadow: {
    sm: "none",
    md: "0 1px 0 rgba(26, 25, 22, 0.06)",
    lg: "0 2px 6px rgba(26, 25, 22, 0.10)",
    xl: "0 6px 20px rgba(26, 25, 22, 0.14)",
    "2xl": "0 10px 32px rgba(26, 25, 22, 0.18)",
  },
  motion: {
    fast: "150ms",
    base: "180ms",
    slow: "220ms",
    page: "220ms",
  },
  font: '"Atkinson Hyperlegible", "Public Sans", -apple-system, "system-ui", "Segoe UI", Helvetica, Arial, "PingFang SC", "Noto Sans CJK SC", sans-serif',
  mono: '"JetBrains Mono", ui-monospace, "SF Mono", "Cascadia Code", "Segoe UI Mono", Menlo, Monaco, Consolas, monospace',
} as const;

export type Theme = typeof theme;
