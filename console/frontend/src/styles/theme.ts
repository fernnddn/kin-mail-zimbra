/** Shared Emotion theme tokens for the console (light UI). */

export const theme = {
  bg: "#f5f7fb",
  bgElev: "#ffffff",
  bgPanel: "#f8fafc",
  sidebar: "#eef2f7",
  ink: "#0f172a",
  muted: "#64748b",
  line: "#e2e8f0",
  accent: "#0061ff",
  accentHover: "#0052d4",
  accentSoft: "rgba(0, 97, 255, 0.10)",
  danger: "#ef4444",
  warn: "#d97706",
  warnSoft: "rgba(217, 119, 6, 0.10)",
  ok: "#059669",
  radius: "8px",
  font: '"IBM Plex Sans", "Avenir Next", "Segoe UI", "Helvetica Neue", sans-serif',
  mono: '"IBM Plex Mono", "SF Mono", ui-monospace, monospace',
  shadow: "0 12px 32px rgba(15, 23, 42, 0.08)",
  motion: "180ms",
} as const;

export type Theme = typeof theme;
