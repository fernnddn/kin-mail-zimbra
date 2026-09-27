/** Whether the contracted seat count is allowed to be left unset.
 *
 * It normally is - the commercial figure often is not known on the day the
 * appliance is built, and the quota gate simply refuses new mailboxes until
 * somebody sets it. That is a fine trade when mailboxes are created by hand.
 *
 * It stops being fine the moment a directory is configured. The Active
 * Directory sync creates a mailbox per person and each one spends a seat, so
 * an unset count refuses every single one: the deploy finishes, authentication
 * is configured correctly, the timer is installed - and the admin console
 * lists nobody, so not one person can sign in.
 *
 * Three deploys in a row ended exactly there (24-26 Sep 2026), because the
 * wizard step called the field optional and offered "Leave blank to set later"
 * as its placeholder. Nothing on the page connected that choice to an empty
 * mailbox list forty minutes later.
 */
/** True when the operator has said outright that there is no cap.
 *
 * A third answer, not a blank one. "Nobody has said yet" and "there is no
 * limit" are opposite, and a gate that reads an empty field as permission is
 * one typo away from being no gate - so this is a word the operator chooses,
 * never an inference from emptiness.
 */
export function seatsAreUnlimited(seats: string): boolean {
  return (seats || "").trim().toLowerCase() === "unlimited";
}

export function seatCountError(opts: {
  seats: string;
  adEnabled: boolean;
}): string {
  const raw = (opts.seats || "").trim();
  if (seatsAreUnlimited(raw)) return "";
  const unset = !raw || raw === "PLACEHOLDER_UNSET";

  if (opts.adEnabled && unset) {
    return (
      "Active Directory is enabled, so this is required. With no seat count " +
      "the directory sync creates nobody and no AD user can sign in."
    );
  }
  if (!unset && !/^\d+$/.test(raw)) {
    return opts.adEnabled
      ? "Contracted mailboxes must be a whole number."
      : "Contracted mailboxes must be a whole number, or left blank.";
  }
  if (opts.adEnabled && raw === "0") {
    return "Zero seats means no mailbox can be created, so no AD user could sign in.";
  }
  return "";
}

/** What to store for a seat field that passed seatCountError. */
export function normaliseSeats(seats: string): string {
  const raw = (seats || "").trim();
  if (seatsAreUnlimited(raw)) return "unlimited";
  return !raw || raw === "PLACEHOLDER_UNSET" ? "PLACEHOLDER_UNSET" : raw;
}
