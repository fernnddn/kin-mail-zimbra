#!/usr/bin/env python3
"""Pick a safe spare disk for DRBD data+meta GPT partitions.

Stdlib only — runs on mail VMs via the drbd_disk_prep role (no kin_privhelper).
Never recommends a disk that is the OS disk, has a partition table, has a
filesystem signature, is mounted, or is below the 20 GiB data floor.

LUKS is applied later (prepare-zimbra-data-disk / drbd_resource). This
selector only plans GPT partitions.

CLI: JSON on stdin → JSON plan on stdout.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

MIN_DATA_BYTES = 20 * 1024**3
MIN_META_BYTES = 128 * 1024**2
MAX_META_BYTES = 2 * 1024**3
META_MIB = 256
# Data partition must still meet the 20 GiB floor after carving meta + alignment.
MIN_DISK_BYTES = MIN_DATA_BYTES + (META_MIB * 1024**2) + (1024**2)

_DEV_RE = re.compile(r"^/dev/[a-zA-Z0-9/._+-]+$")
_PTTYPE_EMPTY = {"", "none", "unknown"}


def parent_disk_name(part_name: str) -> str:
    name = part_name.strip().removeprefix("/dev/")
    if name.startswith("nvme") or name.startswith("mmcblk"):
        return re.sub(r"p\d+$", "", name)
    return re.sub(r"\d+$", "", name)


def partition_paths(disk_path: str) -> tuple[str, str]:
    path = disk_path.rstrip("/")
    name = path.removeprefix("/dev/")
    if name.startswith("nvme") or name.startswith("mmcblk"):
        return f"{path}p1", f"{path}p2"
    return f"{path}1", f"{path}2"


def _norm_dev(value: str) -> str:
    text = (value or "").strip()
    if text.startswith("/dev/mapper/"):
        return text
    if text.startswith("/dev/"):
        return text.split("[", 1)[0].split(" ", 1)[0]
    return text


def _walk(devices: list[dict[str, Any]], acc: list[dict[str, Any]]) -> None:
    for dev in devices:
        if not isinstance(dev, dict):
            continue
        acc.append(dev)
        kids = dev.get("children")
        if isinstance(kids, list):
            _walk(kids, acc)


def flatten_lsblk(lsblk: dict[str, Any]) -> list[dict[str, Any]]:
    block = lsblk.get("blockdevices") if isinstance(lsblk, dict) else None
    if not isinstance(block, list):
        return []
    out: list[dict[str, Any]] = []
    _walk(block, out)
    return out


def dev_path(node: dict[str, Any]) -> str:
    path = str(node.get("path") or "").strip()
    if path:
        return path
    name = str(node.get("name") or "").strip()
    if not name:
        return ""
    return name if name.startswith("/dev/") else f"/dev/{name}"


def _size(node: dict[str, Any]) -> int:
    try:
        return int(node.get("size") or 0)
    except (TypeError, ValueError):
        return 0


def fmt_bytes(n: int) -> str:
    if n >= 1024**3:
        return f"{n / 1024**3:.1f} GiB"
    if n >= 1024**2:
        return f"{n / 1024**2:.0f} MiB"
    return f"{n} B"


def _name(node: dict[str, Any]) -> str:
    return str(node.get("name") or "").strip().removeprefix("/dev/")


def _has_mount(node: dict[str, Any]) -> bool:
    return bool(str(node.get("mountpoint") or "").strip())


def _has_fstype(node: dict[str, Any]) -> bool:
    return bool(str(node.get("fstype") or "").strip())


def _has_pttable(node: dict[str, Any]) -> bool:
    pttype = str(node.get("pttype") or "").strip().lower()
    return bool(pttype) and pttype not in _PTTYPE_EMPTY


def _children(node: dict[str, Any]) -> list[dict[str, Any]]:
    kids = node.get("children")
    return [c for c in kids if isinstance(c, dict)] if isinstance(kids, list) else []


def _is_skip_disk_name(name: str) -> bool:
    return name.startswith(("sr", "fd", "loop", "ram", "zram", "dm-")) or name in {
        "sr0",
        "fd0",
    }


def _root_parent(root_source: str) -> str:
    src = _norm_dev(root_source).removeprefix("/dev/")
    return parent_disk_name(src) if src else ""


def _mounted_on_disk(disk_name: str, nodes: list[dict[str, Any]]) -> list[str]:
    found: list[str] = []
    for node in nodes:
        mp = str(node.get("mountpoint") or "").strip()
        if not mp:
            continue
        nname = _name(node)
        pk = str(node.get("pkname") or "").strip().removeprefix("/dev/")
        if nname == disk_name or pk == disk_name or parent_disk_name(nname) == disk_name:
            found.append(f"{dev_path(node) or nname} on {mp}")
    return found


def _reject_reason(
    node: dict[str, Any],
    *,
    nodes: list[dict[str, Any]],
    root_parent: str,
) -> str:
    if str(node.get("type") or "") != "disk":
        return "not a disk"
    name = _name(node)
    if not name or _is_skip_disk_name(name):
        return "ignored device class"
    if name == root_parent:
        return "backs the root filesystem"
    if _has_mount(node):
        return f"mounted at {node.get('mountpoint')}"
    mounted = _mounted_on_disk(name, nodes)
    if mounted:
        return "has mounted filesystems: " + "; ".join(mounted)
    if _has_fstype(node):
        return f"filesystem signature fstype={node.get('fstype')}"
    if _has_pttable(node):
        return f"existing partition table pttype={node.get('pttype')}"
    parts = [c for c in _children(node) if str(c.get("type") or "") == "part"]
    if parts:
        return "already has partitions: " + " ".join(dev_path(c) or _name(c) for c in parts)
    if _size(node) < MIN_DISK_BYTES:
        return (
            f"too small ({fmt_bytes(_size(node))}, need ≥{fmt_bytes(MIN_DISK_BYTES)} "
            "so data stays ≥20 GiB after 256 MiB meta)"
        )
    return ""


def find_candidates(
    lsblk: dict[str, Any],
    root_source: str,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Return (safe blank disks, skipped disks with reasons)."""
    nodes = flatten_lsblk(lsblk)
    root_parent = _root_parent(root_source)
    ok: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for node in nodes:
        if str(node.get("type") or "") != "disk":
            continue
        path = dev_path(node)
        name = _name(node)
        if not path or _is_skip_disk_name(name):
            continue
        why = _reject_reason(node, nodes=nodes, root_parent=root_parent)
        if why:
            skipped.append({"path": path, "reason": why})
            continue
        ok.append(
            {
                "path": path,
                "name": name,
                "size": _size(node),
                "size_human": fmt_bytes(_size(node)),
            }
        )
    return ok, skipped


def layout_ready(
    lsblk: dict[str, Any],
    *,
    data_disk: str,
    meta_disk: str,
    root_source: str,
) -> dict[str, Any] | None:
    """Return a skip-plan if data+meta already exist with the proven sizes."""
    data_disk = _norm_dev(data_disk)
    meta_disk = _norm_dev(meta_disk)
    nodes = {dev_path(n): n for n in flatten_lsblk(lsblk) if dev_path(n)}
    data_node = nodes.get(data_disk)
    meta_node = nodes.get(meta_disk)
    if data_node is None or meta_node is None:
        return None
    if str(data_node.get("type") or "") not in ("part", "lvm"):
        return None
    if str(meta_node.get("type") or "") not in ("part", "lvm"):
        return None
    if _size(data_node) < MIN_DATA_BYTES:
        return None
    sz = _size(meta_node)
    if sz < MIN_META_BYTES or sz > MAX_META_BYTES:
        return None
    if _has_mount(meta_node):
        return None
    root_parent = _root_parent(root_source)
    if root_parent and parent_disk_name(data_disk.removeprefix("/dev/")) == root_parent:
        return None
    parent = f"/dev/{parent_disk_name(data_disk.removeprefix('/dev/'))}"
    return {
        "action": "skip",
        "disk": parent,
        "data_disk": data_disk,
        "meta_disk": meta_disk,
        "size": _size(nodes.get(parent) or {}),
        "size_human": fmt_bytes(_size(nodes.get(parent) or {})),
        "message": (
            f"DRBD layout already present: {data_disk} (data) + {meta_disk} (meta) "
            "on {parent} — not re-partitioning."
        ).replace("{parent}", parent),
        "errors": [],
        "candidates": [],
        "skipped": [],
    }


def plan_auto_partition(
    lsblk: dict[str, Any],
    *,
    root_source: str,
    data_disk: str = "/dev/sdb1",
    meta_disk: str = "/dev/sdb2",
) -> dict[str, Any]:
    data_disk = _norm_dev(data_disk) or "/dev/sdb1"
    meta_disk = _norm_dev(meta_disk) or "/dev/sdb2"
    if not _DEV_RE.match(data_disk) or not _DEV_RE.match(meta_disk):
        return {
            "action": "fail",
            "errors": ["internal: DRBD disk path is not a /dev/ device"],
            "candidates": [],
            "skipped": [],
            "message": "Refusing: DRBD disk path is not a /dev/ device.",
        }

    ready = layout_ready(
        lsblk, data_disk=data_disk, meta_disk=meta_disk, root_source=root_source
    )
    if ready:
        return ready

    candidates, skipped = find_candidates(lsblk, root_source)
    expected_parent = f"/dev/{parent_disk_name(data_disk.removeprefix('/dev/'))}"
    cand_paths = [str(c["path"]) for c in candidates]
    listed = ", ".join(
        f"{c['path']} ({c['size_human']})" for c in candidates
    ) or "(none)"
    skipped_txt = "; ".join(f"{s['path']}: {s['reason']}" for s in skipped)

    def fail(errors: list[str], message: str) -> dict[str, Any]:
        return {
            "action": "fail",
            "disk": "",
            "data_disk": data_disk,
            "meta_disk": meta_disk,
            "errors": errors,
            "candidates": candidates,
            "skipped": skipped,
            "message": message,
        }

    if not candidates:
        extra = f" Other disks: {skipped_txt}." if skipped_txt else ""
        return fail(
            [
                f"no spare unpartitioned disk found (need one blank disk ≥"
                f"{fmt_bytes(MIN_DISK_BYTES)} that is not the OS disk, has no "
                f"partition table, no filesystem signature, and is not mounted)."
                f"{extra}"
            ],
            "Refusing: no spare unpartitioned disk found." + extra,
        )

    if len(candidates) > 1:
        return fail(
            [
                "more than one spare unpartitioned disk found — refusing to guess: "
                + listed
                + f". Set KIN_DRBD_DATA_DISK to the intended data partition "
                f"(expected parent {expected_parent})."
            ],
            f"Refusing: multiple spare disks {listed}. Specify the DRBD disk explicitly.",
        )

    chosen = candidates[0]
    if str(chosen["path"]) != expected_parent:
        return fail(
            [
                f"unique spare disk is {chosen['path']} but Build HA expects partitions "
                f"on {expected_parent} ({data_disk} / {meta_disk}). Refusing to guess. "
                f"Set KIN_DRBD_DATA_DISK to {partition_paths(str(chosen['path']))[0]} "
                f"if that disk is the intended DRBD data device."
            ],
            f"Refusing: unique spare {chosen['path']} is not {expected_parent}.",
        )

    p1, p2 = partition_paths(str(chosen["path"]))
    msg = (
        f"Selected {chosen['path']} ({chosen['size_human']}) — not the OS disk, "
        f"no partition table, no filesystem signature, not mounted, "
        f"≥{fmt_bytes(MIN_DISK_BYTES)}. Will GPT-partition "
        f"{p1} (data, remaining minus {META_MIB} MiB meta) and "
        f"{p2} (~{META_MIB} MiB DRBD meta, no mkfs)."
    )
    return {
        "action": "partition",
        "disk": chosen["path"],
        "data_disk": p1,
        "meta_disk": p2,
        "size": chosen["size"],
        "size_human": chosen["size_human"],
        "meta_mib": META_MIB,
        "errors": [],
        "candidates": candidates,
        "skipped": skipped,
        "message": msg,
        "parted_argv": [
            "parted",
            "-s",
            "--",
            str(chosen["path"]),
            "mklabel",
            "gpt",
            "mkpart",
            "zimbra-data",
            "1MiB",
            f"-{META_MIB}MiB",
            "mkpart",
            "drbd-meta",
            f"-{META_MIB}MiB",
            "100%",
        ],
    }


def install_prepare_plan(
    lsblk: dict[str, Any],
    *,
    root_source: str,
    data_disk: str = "/dev/sdb1",
    meta_disk: str = "/dev/sdb2",
) -> dict[str, Any]:
    """Map plan_auto_partition onto full-install: prepare_data or os_root.

    HA disk_prep still fail-closes on action=fail. Install must not: no spare,
    two spares, or a unique spare that is not the expected parent all mean
    "install Zimbra on the OS volume as today". Only partition/skip become
    a data-disk prepare. parted_argv is required for partition.
    """
    plan = plan_auto_partition(
        lsblk,
        root_source=root_source,
        data_disk=data_disk,
        meta_disk=meta_disk,
    )
    action = str(plan.get("action") or "")
    base = {
        "plan": plan,
        "data_disk": str(plan.get("data_disk") or data_disk),
        "meta_disk": str(plan.get("meta_disk") or meta_disk),
        "disk": str(plan.get("disk") or ""),
        "parted_argv": list(plan.get("parted_argv") or []),
    }
    if action == "partition":
        argv = base["parted_argv"]
        if not argv or str(argv[0]) != "parted":
            return {
                **base,
                "install_mode": "os_root",
                "need_partition": False,
                "reason": (
                    "selector returned partition without a parted argv; "
                    "refusing to guess. Zimbra will install on the OS volume."
                ),
            }
        return {
            **base,
            "install_mode": "prepare_data",
            "need_partition": True,
            "reason": str(plan.get("message") or "unique blank spare disk"),
        }
    if action == "skip":
        return {
            **base,
            "install_mode": "prepare_data",
            "need_partition": False,
            "reason": str(plan.get("message") or "proven GPT layout already present"),
        }
    return {
        **base,
        "install_mode": "os_root",
        "need_partition": False,
        "reason": str(plan.get("message") or "no unique spare disk"),
    }


def main() -> int:
    raw = sys.stdin.read()
    try:
        req = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        json.dump(
            {"action": "fail", "errors": [f"invalid JSON stdin: {exc}"], "message": str(exc)},
            sys.stdout,
        )
        return 2
    if not isinstance(req, dict):
        json.dump(
            {"action": "fail", "errors": ["stdin JSON must be an object"], "message": ""},
            sys.stdout,
        )
        return 2
    lsblk = req.get("lsblk") if isinstance(req.get("lsblk"), dict) else {}
    root_source = str(req.get("root_source") or "")
    data_disk = str(req.get("data_disk") or "/dev/sdb1")
    meta_disk = str(req.get("meta_disk") or "/dev/sdb2")
    mode = str(req.get("mode") or "").strip().lower()
    if mode == "install":
        out = install_prepare_plan(
            lsblk,
            root_source=root_source,
            data_disk=data_disk,
            meta_disk=meta_disk,
        )
        json.dump(out, sys.stdout)
        sys.stdout.write("\n")
        return 0
    plan = plan_auto_partition(
        lsblk,
        root_source=root_source,
        data_disk=data_disk,
        meta_disk=meta_disk,
    )
    json.dump(plan, sys.stdout)
    sys.stdout.write("\n")
    return 0 if plan.get("action") != "fail" else 2


if __name__ == "__main__":
    sys.exit(main())
