"""Classify an empty Pacemaker CIB skeleton (no live Pacemaker).

When the Debian/Ubuntu corosync package stub auto-starts Pacemaker, the
daemon may write /var/lib/pacemaker/cib/cib.xml as a minimal empty document
(no nodes, no resources, no status history). pcs cluster setup then refuses
with "cluster configuration files have been found" even though there is no
real membership.

Canonical copy: imported by the Ansible filter plugin at
ansible/roles/cluster_setup/filter_plugins/pacemaker_cib.py.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any


def _local_tag(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _element_children(parent: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in list(parent) if _local_tag(child.tag) == name]


def _has_element_children(parent: ET.Element) -> bool:
    return any(True for _ in list(parent))


def _section_has_payload(config: ET.Element, name: str) -> bool:
    for section in _element_children(config, name):
        if _has_element_children(section):
            return True
    return False


def _status_has_history(root: ET.Element) -> bool:
    """True when status records prior membership or LRM resource history."""
    for status in _element_children(root, "status"):
        for node_state in _element_children(status, "node_state"):
            # Any node_state means the CIB saw a live member join.
            return True
        if _has_element_children(status):
            return True
    return False


def is_empty_skeleton_cib(text: Any) -> bool:
    """True only for a parseable CIB with no nodes, resources, or history.

    Fail closed on missing text, parse errors, unexpected root tags, or any
    configuration/status payload that could belong to a real cluster.
    crm_config bootstrap nvpairs alone are allowed (package first-start).
    """
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    if not isinstance(text, str) or not text.strip():
        return False

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return False

    if _local_tag(root.tag) != "cib":
        return False

    configs = _element_children(root, "configuration")
    if len(configs) != 1:
        return False
    config = configs[0]

    # A package-first-start CIB always has empty nodes and resources sections.
    # Missing sections are not a confirmed skeleton (fail closed).
    for required in ("nodes", "resources"):
        sections = _element_children(config, required)
        if len(sections) != 1 or _has_element_children(sections[0]):
            return False

    # Real membership or workload leaves children under these sections.
    for section in (
        "constraints",
        "tags",
        "alerts",
        "fencing-topology",
        "op_defaults",
        "rsc_defaults",
    ):
        if _section_has_payload(config, section):
            return False

    if _status_has_history(root):
        return False

    return True
