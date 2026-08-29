"""Carry a license decision to the other mail node.

An HA pair is one licensed appliance, not two. Both nodes serve the same
mailboxes off the same replicated volume, so a seat count that applied to only
one of them would be enforced or not depending on which node the VIP happened
to point at, and the operator would see the license appear and disappear as the
cluster failed over.

`Build HA pair` already copies server-id, license.token and session.secret to
the peer, but only while the pair is being built. Applying a license is
something you do afterwards, on a pair that is already running, and nothing
carried it across: the operator applied a key on one node and had to repeat it
by hand on the other (Phase 8, 29 Aug 2026).

Two things have to travel together, in this order:

  CONTRACTED_SEATS in /etc/kin-mail/config, which is what the quota gate reads
  license.token, which is what the console reads

Seats first. A node holding a verified token with a stale seat count reads as
licensed while still enforcing the old limit, which is the one combination that
is worse than not syncing at all. Writing the token last means an interruption
leaves the peer under-licensed and visibly so, never over-licensed and silent.
"""

from __future__ import annotations

from typing import Any

from .orchestration import OrchHost

# The peer's config is rewritten from its own contents, never from this node's,
# so peer-specific values survive. These two must be present in what comes back
# or the read did not return a real config and nothing is written.
_REQUIRED_PEER_KEYS = ("MAIL_HOST", "SERVER_IP")

LICENSE_TOKEN_DEST = "/var/lib/kin-mail-console/license.token"
SERVER_ID_DEST = "/etc/kin-mail/server-id"
MAIL_CONFIG_DEST = "/etc/kin-mail/config"


def plan_license_sync(
    *,
    topology: str,
    peer_name: str,
    peer_ip: str,
) -> dict[str, Any]:
    """Decide whether this license change has to reach a second node.

    No I/O. `single` means there is nothing to sync and the local write is the
    whole job; `refuse` means there is a peer but no way to reach it, which the
    caller must report rather than silently leaving the pair disagreeing.
    """
    topo = (topology or "").strip().lower()
    clustered = topo in ("2vm", "2")
    if not clustered:
        return {"action": "single", "reason": "single-node appliance"}
    if not (peer_ip or "").strip():
        return {
            "action": "refuse",
            "error": (
                "This is a 2-server appliance but the peer address is unknown, "
                "so the license could not be applied to the other node. "
                "It is applied here only."
            ),
        }
    return {
        "action": "push",
        "peer_name": (peer_name or "").strip(),
        "peer_ip": peer_ip.strip(),
    }


def peer_config_with_seats(peer_config_text: str, seats: str) -> tuple[str, str]:
    """Return (body, error). Rewrites only CONTRACTED_SEATS in the peer's own config.

    The whole file is re-emitted through the same formatter the local writer
    uses, from the peer's own values, so nothing node-specific is carried over
    from here. A read that does not look like a config is refused outright: an
    empty or truncated body would otherwise be formatted into a valid-looking
    file that has lost every setting the peer had.
    """
    from .apply_config import format_config, parse_config

    seats = str(seats).strip()
    if not seats.isdigit() or int(seats) < 1:
        return "", f"refusing to write a non-numeric seat count ({seats!r}) to the peer"
    if not (peer_config_text or "").strip():
        return "", "the peer returned an empty /etc/kin-mail/config"
    parsed = parse_config(peer_config_text)
    missing = [k for k in _REQUIRED_PEER_KEYS if not str(parsed.get(k) or "").strip()]
    if missing:
        return "", (
            "the peer's /etc/kin-mail/config is missing "
            + ", ".join(missing)
            + "; refusing to rewrite it"
        )
    parsed["CONTRACTED_SEATS"] = seats
    return format_config(parsed), ""


async def resolve_peer() -> dict[str, Any]:
    """Look up the peer for a license push. Returns a plan_license_sync result."""
    from .console_users_sync import _host_ip, _mail_config
    from .deploy_state import saved_wizard_topology
    from .maintenance import gather_status

    config = _mail_config()
    try:
        st = await gather_status()
    except Exception:  # noqa: BLE001 - status is a convenience here, not a gate
        st = {}
    addrs = dict((st or {}).get("addrs") or {})
    peer_name = str(config.get("PEER_HOST_NAME") or "").strip()
    peer_ip = _host_ip(
        peer_name,
        addrs=addrs,
        config=config,
        local_ip=str(config.get("SERVER_IP") or ""),
    ) or str(config.get("PEER_HOST_IP") or "").strip()
    return plan_license_sync(
        topology=saved_wizard_topology(),
        peer_name=peer_name,
        peer_ip=peer_ip,
    )


async def push_license_to_peer(
    *,
    seats: str,
    token: str,
    server_id: str,
) -> tuple[bool, list[str]]:
    """Apply this node's license decision to the peer. Returns (ok, lines).

    The token is never logged. Failure is reported, never swallowed: an
    operator who is told the license applied has to be able to believe it of
    the whole appliance.
    """
    from .console_users_sync import _ssh_session
    from .orchestration import _push_peer_text_file, _ssh_run

    lines: list[str] = []
    plan = await resolve_peer()
    if plan["action"] == "single":
        return True, lines
    if plan["action"] == "refuse":
        return False, [str(plan["error"])]

    host = OrchHost(name=plan["peer_name"] or plan["peer_ip"], ip=plan["peer_ip"])
    label = plan["peer_name"] or plan["peer_ip"]
    try:
        user, password, secrets = await _ssh_session(host)
    except RuntimeError:
        return False, [
            f"Could not log in to {label} to apply the license there. "
            "It is applied on this node only."
        ]

    # server-id first: the token is verified against it, so a peer carrying a
    # different id would reject a token that is correct for this appliance.
    # Pairs built before this was synced are repaired here rather than left to
    # fail confusingly at the next status read.
    code, _text = await _push_peer_text_file(
        host, user, password, secrets,
        body=server_id + "\n",
        remote_tmp="/tmp/kin-mail-peer-server-id",
        dest=SERVER_ID_DEST,
        mode="644",
    )
    if code != 0:
        return False, [
            f"Could not write the appliance id on {label} (exit {code}). "
            "The license is applied on this node only."
        ]

    code, peer_conf = await _ssh_run(
        host, user, password, secrets, f"cat {MAIL_CONFIG_DEST}", timeout=20
    )
    if code != 0:
        return False, [
            f"Could not read {MAIL_CONFIG_DEST} on {label} (exit {code}). "
            "The license is applied on this node only."
        ]
    body, err = peer_config_with_seats(peer_conf, seats)
    if err:
        return False, [f"Seat count not applied to {label}: {err}."]
    code, _text = await _push_peer_text_file(
        host, user, password, secrets,
        body=body,
        remote_tmp="/tmp/kin-mail-peer-config",
        dest=MAIL_CONFIG_DEST,
        mode="600",
    )
    if code != 0:
        return False, [
            f"Could not write the seat count on {label} (exit {code}). "
            "The license is applied on this node only."
        ]
    lines.append(f"Applied {seats} seats on {label}")

    if token:
        code, _text = await _push_peer_text_file(
            host, user, password, secrets,
            body=token + "\n",
            remote_tmp="/tmp/kin-mail-peer-license-token",
            dest=LICENSE_TOKEN_DEST,
            mode="600",
            owner="kin-console",
            group="kin-console",
        )
        if code != 0:
            return False, [
                f"Seats were applied on {label} but the license key was not "
                f"(exit {code}). That node will report itself unlicensed until "
                "this is retried."
            ]
        lines.append(f"Applied the license key on {label}")
    return True, lines
