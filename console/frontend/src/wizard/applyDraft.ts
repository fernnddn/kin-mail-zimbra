/** The apply_draft log names the real refusal. The banner used to hide it. */
export function applyDraftFailureMessage(log: string): string {
  const prefix = "Could not save settings, Deploy stopped before install.";
  const reasons = log
    .split("\n")
    .map((ln) => ln.trim())
    .filter((ln) => ln.startsWith("ERROR:") || ln.startsWith("- "))
    .slice(0, 6);
  if (!reasons.length) {
    return `${prefix} Open the log below for the reason.`;
  }
  const detail = reasons.join(" ").replace(/^ERROR:\s*/i, "");
  return `${prefix} ${detail}`;
}
