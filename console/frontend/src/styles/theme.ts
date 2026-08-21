/** Shared Emotion theme tokens for the console (light UI only).
 * Palette, radius, shadow, and motion match the KIN ERP admin frontend.
 */

export const primary = {
  50: "#edf5ff",
  100: "#d9e8ff",
  200: "#bcd7ff",
  300: "#8ebfff",
  400: "#599bff",
  500: "#0061ff",
  600: "#0061ff",
  700: "#0052d4",
  800: "#003e9e",
  900: "#002a66",
} as const;

export const surface = {
  50: "#f8fafc",
  100: "#f1f5f9",
  200: "#e2e8f0",
  300: "#cbd5e1",
  400: "#94a3b8",
  500: "#64748b",
  600: "#475569",
  700: "#334155",
  800: "#1e293b",
  900: "#0f172a",
} as const;

export const theme = {
  primary,
  surface,

  critical: "#ef4444",
  high: "#f97316",
  medium: "#eab308",
  low: "#22c55e",
  completed: "#10b981",
  blueprint: "#0ea5e9",

  bg: surface[50],
  bgElev: "#ffffff",
  bgPanel: surface[50],
  sidebar: "#eef2f7",
  ink: surface[800],
  muted: surface[500],
  line: surface[200],
  accent: primary[500],
  accentHover: primary[700],
  accentSoft: "rgba(0, 97, 255, 0.10)",
  danger: "#ef4444",
  warn: "#d97706",
  warnSoft: "rgba(217, 119, 6, 0.10)",
  ok: "#10b981",

  radius: {
    sm: "6px",
    md: "8px",
    lg: "8px",
    xl: "12px",
    "2xl": "16px",
    full: "999px",
  },
  shadow: {
    sm: "0 1px 2px rgba(15, 23, 42, 0.06)",
    md: "0 4px 12px rgba(15, 23, 42, 0.08)",
    lg: "0 8px 24px rgba(15, 23, 42, 0.10)",
    xl: "0 16px 36px rgba(15, 23, 42, 0.14)",
    "2xl": "0 24px 48px rgba(15, 23, 42, 0.18)",
  },
  motion: {
    fast: "150ms",
    base: "200ms",
    slow: "250ms",
    page: "350ms",
  },
  font: '"Inter", "Inter var", "PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC", -apple-system, "system-ui", "Segoe UI", Helvetica, Arial, sans-serif',
  mono: '"JetBrains Mono", ui-monospace, "SF Mono", "Cascadia Code", "Segoe UI Mono", Menlo, Monaco, Consolas, monospace',
} as const;

export type Theme = typeof theme;
