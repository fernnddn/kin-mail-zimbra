/**
 * Features that exist in the tree but are not offered to operators yet.
 *
 * These are not dead code and must not be deleted. Everything behind a false
 * flag here is complete enough to run and not finished enough to sell, and it
 * stays in the build so it can be brought back by flipping one value rather
 * than resurrected from history.
 */

/**
 * The two-server, high-availability layout: DRBD replication, Pacemaker,
 * a quorum witness, a floating VIP, Build HA pair, Add host.
 *
 * Hidden for the 0.1.9 release. The single-server appliance is what this
 * release was hardened for and what is being sold; the pair is real but not
 * yet mature, and an operator who found the button, pressed it, and hit a
 * rough edge would reasonably conclude the whole product is rough. Hiding it
 * is the honest version of "not yet", where leaving it visible is an implied
 * promise the release cannot keep.
 *
 * Turning this back to true restores every entry point at once: the wizard's
 * topology choice, Build HA pair on the deploy step, Add host, and the
 * Add-a-second-server action on the Cluster page. Nothing else needs editing.
 *
 * Note what this does NOT do. It hides the paths that would START building a
 * pair. An appliance that is already a pair keeps every screen it needs:
 * status, health, maintenance and removal all still work, because hiding the
 * controls for a cluster somebody is already running would be a far worse
 * failure than showing a button too early.
 */
export const HA_TOPOLOGY_OFFERED = false;
