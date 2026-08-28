/** Strip ANSI CSI / OSC sequences for clean plain-text log viewing. */
const ANSI_RE =
  // eslint-disable-next-line no-control-regex -- intentional control-char strip
  /\u001b\[[0-9;?]*[ -/]*[@-~]|\u001b\][^\u0007]*(?:\u0007|\u001b\\)|\u001b[@-Z\\-_]/g;

// A colour code whose ESC byte did not survive whatever carried it here.
// "\u001b[36m" becomes a bare "[36m", which renders as literal noise. The
// pattern is deliberately narrow - digits and semicolons ending in m - so it
// cannot eat ordinary bracketed text out of a log line.
// eslint-disable-next-line no-control-regex -- see above
const ORPHAN_SGR_RE = /\[[0-9;]{1,7}m/g;

export function stripAnsi(text: string): string {
  return text.replace(ANSI_RE, "").replace(ORPHAN_SGR_RE, "");
}

/** Normalize install log lines for left-aligned readable plain text. */
export function formatDeployLog(text: string): string {
  return stripAnsi(text)
    .replace(/\r\n/g, "\n")
    .replace(/\r/g, "\n")
    .split("\n")
    .map((line) => line.replace(/[ \t]+$/g, ""))
    .join("\n")
    .replace(/^\n+/, "");
}
