/** Shared Emotion theme tokens for the console (light UI).
 * Font stack chosen for visual consistency:
 *   font-family: DM Sans, InterVariable, Inter, -apple-system, …, sans-serif
 * Motion is a short ~0.3s ease-out on interactive elements.
 */

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
  font: '"DM Sans", InterVariable, Inter, -apple-system, "system-ui", "Segoe UI", Helvetica, Arial, sans-serif',
  mono: 'ui-monospace, "SF Mono", "Cascadia Code", "Segoe UI Mono", Menlo, Monaco, Consolas, monospace',
  shadow: "0 12px 32px rgba(15, 23, 42, 0.08)",
  motion: "300ms",
} as const;

export type Theme = typeof theme;
