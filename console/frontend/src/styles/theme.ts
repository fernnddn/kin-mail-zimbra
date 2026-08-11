/** Shared Emotion theme tokens for the console. */

export const theme = {
  bg: "#0e1318",
  bgElev: "#161d26",
  bgPanel: "#121820",
  sidebar: "#0b1015",
  ink: "#e7eef6",
  muted: "#8b9aab",
  line: "#2a3542",
  accent: "#2f6fed",
  accentHover: "#4580f5",
  accentSoft: "rgba(47, 111, 237, 0.14)",
  danger: "#d64545",
  warn: "#c9852a",
  warnSoft: "rgba(201, 133, 42, 0.12)",
  ok: "#3d9a6a",
  radius: "8px",
  font: '"IBM Plex Sans", "Avenir Next", "Segoe UI", "Helvetica Neue", sans-serif',
  mono: '"IBM Plex Mono", "SF Mono", ui-monospace, monospace',
  shadow: "0 18px 48px rgba(0, 0, 0, 0.35)",
} as const;

export type Theme = typeof theme;
