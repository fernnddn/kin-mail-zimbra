/** Dates an operator reads, not the wire format they arrived in. */

/**
 * "28 Aug 2027" from an ISO timestamp.
 *
 * These come off the licence and the certificate as full ISO 8601 with an
 * offset - "2027-08-28T10:35:08+00:00" - which is right for a machine and
 * hard work for a person scanning a settings page.
 *
 * Anything unparseable is handed back untouched: a date we cannot read is
 * still better shown raw than replaced with "Invalid Date".
 */
export function formatDay(iso: string | null | undefined): string {
  if (!iso) return "";
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return String(iso);
  return at.toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

/** Whole days from now until `iso`; negative once it has passed. */
export function daysUntil(iso: string | null | undefined, now = Date.now()): number | null {
  if (!iso) return null;
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return null;
  return Math.floor((at.getTime() - now) / 86400000);
}

/** "28 Aug 2027 (364 days left)", or "(expired 3 days ago)" once past. */
export function formatExpiry(iso: string | null | undefined, now = Date.now()): string {
  const day = formatDay(iso);
  if (!day) return "";
  const left = daysUntil(iso, now);
  if (left === null) return day;
  if (left < 0) {
    const ago = Math.abs(left);
    return `${day} (expired ${ago} day${ago === 1 ? "" : "s"} ago)`;
  }
  if (left === 0) return `${day} (expires today)`;
  return `${day} (${left} day${left === 1 ? "" : "s"} left)`;
}

/** Renewal is worth doing something about at this point. */
export const EXPIRY_WARN_DAYS = 30;

export function expiringSoon(
  iso: string | null | undefined,
  now = Date.now(),
  within = EXPIRY_WARN_DAYS,
): boolean {
  const left = daysUntil(iso, now);
  return left !== null && left <= within;
}
