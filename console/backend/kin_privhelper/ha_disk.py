"""Read-only DRBD backing-disk preflight for wizard HA build.

The Ansible drbd_resource role assumes {{ drbd_resource_disk }} (default
/dev/sdb1) already exists. Nothing in this repo partitions a raw second disk.
This module only inspects; it never writes a partition table, mkfs, or mount.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any

# Proven HA layout (ha-build-03 / HA-RUNBOOK): data + small external meta.
DEFAULT_DATA_DISK = "/dev/sdb1"
DEFAULT_META_DISK = "/dev/sdb2"
MIN_DATA_BYTES = 20 * 1024**3  # 20 GiB floor — not a sizing recommendation
MIN_META_BYTES = 128 * 1024**2
MAX_META_BYTES = 2 * 1024**3

_DEV_RE = re.compile(r"^/dev/[a-zA-Z0-9/._+-]+$")


def data_disk_path() -> str:
    return str(os.environ.get("KIN_DRBD_DATA_DISK") or DEFAULT_DATA_DISK).strip() or DEFAULT_DATA_DISK


def meta_disk_path() -> str:
    return str(os.environ.get("KIN_DRBD_META_DISK") or DEFAULT_META_DISK).strip() or DEFAULT_META_DISK


def _norm_dev(value: str) -> str:
    text = (value or "").strip()
    if text.startswith("/dev/mapper/"):
        return text
    # findmnt may return "/dev/sda2[ /]" or UUID=… — keep /dev/* prefix only.
    if text.startswith("/dev/"):
        return text.split("[", 1)[0].split(" ", 1)[0]
    return text


def _parent_disk_name(part_name: str) -> str:
    name = part_name.strip().removeprefix("/dev/")
    if name.startswith("nvme") or name.startswith("mmcblk"):
        return re.sub(r"p\d+$", "", name)
    return re.sub(r"\d+$", "", name)


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


def _dev_path(node: dict[str, Any]) -> str:
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


def _fmt_bytes(n: int) -> str:
    if n >= 1024**3:
        return f"{n / 1024**3:.1f} GiB"
    if n >= 1024**2:
        return f"{n / 1024**2:.0f} MiB"
    return f"{n} B"


def summarize_disks(lsblk: dict[str, Any], *, root_source: str) -> list[str]:
    """Human-readable extra disks (not the OS disk) for error messages."""
    root_parent = _parent_disk_name(_norm_dev(root_source).removeprefix("/dev/")) if root_source else ""
    lines: list[str] = []
    for node in flatten_lsblk(lsblk):
        if str(node.get("type") or "") != "disk":
            continue
        path = _dev_path(node)
        name = str(node.get("name") or "").strip().removeprefix("/dev/")
        if name == root_parent or path.rstrip("0123456789") == f"/dev/{root_parent}":
            continue
        kids = node.get("children") if isinstance(node.get("children"), list) else []
        parts = [
            _dev_path(c)
            for c in kids
            if isinstance(c, dict) and str(c.get("type") or "") == "part"
        ]
        pttype = node.get("pttype") or "none"
        if parts:
            lines.append(
                f"{path} ({_fmt_bytes(_size(node))}, table={pttype}, parts={' '.join(parts)})"
            )
        else:
            lines.append(f"{path} ({_fmt_bytes(_size(node))}, unpartitioned)")
    return lines


def evaluate_node(
    *,
    lsblk: dict[str, Any],
    root_source: str,
    zimbra_source: str,
    zimbra_exists: bool,
    require_zimbra_on_data: bool,
    data_disk: str = DEFAULT_DATA_DISK,
    meta_disk: str = DEFAULT_META_DISK,
    label: str = "this server",
) -> dict[str, Any]:
    """Return a JSON-serialisable result. ok=False is fail-closed."""
    data_disk = _norm_dev(data_disk) or DEFAULT_DATA_DISK
    meta_disk = _norm_dev(meta_disk) or DEFAULT_META_DISK
    if not _DEV_RE.match(data_disk) or not _DEV_RE.match(meta_disk):
        return {
            "ok": False,
            "label": label,
            "errors": ["internal: DRBD disk path is not a /dev/ device"],
            "seen": [],
        }

    nodes = {_dev_path(n): n for n in flatten_lsblk(lsblk) if _dev_path(n)}
    seen = summarize_disks(lsblk, root_source=root_source)
    errors: list[str] = []

    data_parent = f"/dev/{_parent_disk_name(data_disk.removeprefix('/dev/'))}"
    data_node = nodes.get(data_disk)
    parent_node = nodes.get(data_parent)

    if data_node is None:
        if parent_node is not None and str(parent_node.get("type") or "") == "disk":
            errors.append(
                f"{label}: second disk not partitioned — found {data_parent} but not {data_disk}. "
                f"Create GPT partitions {data_disk} (Zimbra/DRBD data, ≥{_fmt_bytes(MIN_DATA_BYTES)}) "
                f"and {meta_disk} (~256 MiB DRBD meta). Do not mkfs the meta partition. "
                "This console will not partition disks (wrong-disk selection is destructive)."
            )
        else:
            extra = f" Seen: {'; '.join(seen)}." if seen else " No extra disk was visible to lsblk."
            errors.append(
                f"{label}: second disk not found — expected DRBD data partition {data_disk}. "
                f"Attach a second disk and partition it before Build HA pair.{extra}"
            )
    else:
        if str(data_node.get("type") or "") not in ("part", "lvm"):
            errors.append(
                f"{label}: {data_disk} exists but is type={data_node.get('type')!r}, "
                "not a partition. Partition the second disk; do not pass the whole disk to DRBD."
            )
        elif _size(data_node) < MIN_DATA_BYTES:
            errors.append(
                f"{label}: {data_disk} is {_fmt_bytes(_size(data_node))}, "
                f"below the {_fmt_bytes(MIN_DATA_BYTES)} floor for DRBD/Zimbra data."
            )
        root_n = _norm_dev(root_source)
        if root_n and _parent_disk_name(data_disk.removeprefix("/dev/")) == _parent_disk_name(
            root_n.removeprefix("/dev/")
        ):
            errors.append(
                f"{label}: {data_disk} is on the OS disk — refusing to use it for DRBD."
            )

    meta_node = nodes.get(meta_disk)
    if meta_node is None:
        if data_node is not None or parent_node is not None:
            errors.append(
                f"{label}: DRBD meta partition {meta_disk} not found (~256 MiB, no filesystem). "
                "The proven layout is data + small external meta on the same second disk."
            )
    else:
        sz = _size(meta_node)
        if sz < MIN_META_BYTES:
            errors.append(
                f"{label}: {meta_disk} is {_fmt_bytes(sz)}, too small for DRBD meta "
                f"(need ≥{_fmt_bytes(MIN_META_BYTES)})."
            )
        elif sz > MAX_META_BYTES:
            errors.append(
                f"{label}: {meta_disk} is {_fmt_bytes(sz)}, too large to be the meta partition "
                f"(expected ~256 MiB, max {_fmt_bytes(MAX_META_BYTES)})."
            )
        mp = str(meta_node.get("mountpoint") or "").strip()
        if mp:
            errors.append(
                f"{label}: {meta_disk} is mounted on {mp} — meta must stay unformatted/unmounted."
            )

    zimbra_src = _norm_dev(zimbra_source)
    if require_zimbra_on_data and zimbra_exists:
        allowed = {data_disk, "/dev/drbd0", "/dev/drbd/by-res/kin-zimbra"}
        if zimbra_src.startswith("/dev/drbd"):
            pass
        elif zimbra_src not in allowed:
            where = zimbra_src or "the root volume"
            errors.append(
                f"{label}: Zimbra is on {where}, not {data_disk}. Build HA pair would replicate "
                "an empty DRBD disk and leave mail on the OS volume. Move /opt/zimbra onto "
                f"{data_disk} first (stop Zimbra, rsync, fstab UUID, start — see HA-RUNBOOK)."
            )

    return {
        "ok": not errors,
        "label": label,
        "data_disk": data_disk,
        "meta_disk": meta_disk,
        "root_source": _norm_dev(root_source),
        "zimbra_source": zimbra_src,
        "zimbra_exists": zimbra_exists,
        "seen": seen,
        "errors": errors,
    }


def combine_results(*nodes: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    for node in nodes:
        errors.extend(str(e) for e in (node.get("errors") or []))
    return {
        "ok": all(bool(n.get("ok")) for n in nodes) if nodes else False,
        "nodes": list(nodes),
        "errors": errors,
        "instructions": (
            "On each mail VM: attach a second unused disk, then GPT-partition "
            f"{data_disk_path()} (ext4 Zimbra data, ≥{_fmt_bytes(MIN_DATA_BYTES)}) and "
            f"{meta_disk_path()} (~256 MiB, no mkfs). If Zimbra is already on the root "
            "volume, migrate /opt/zimbra onto the data partition before Build HA pair. "
            "This wizard will not partition or format disks."
        ),
    }


REMOTE_PROBE = (
    "echo '---LSBLK---'; "
    "lsblk -J -b -o NAME,PATH,TYPE,SIZE,FSTYPE,MOUNTPOINT,PKNAME,PTTYPE; "
    "echo '---ROOT---'; "
    "findmnt -n -o SOURCE /; "
    "echo '---ZIMBRA---'; "
    "findmnt -n -o SOURCE /opt/zimbra 2>/dev/null || true; "
    "echo '---ZIMBRA_DIR---'; "
    "if [ -d /opt/zimbra ]; then echo yes; else echo no; fi"
)


def parse_remote_probe(text: str) -> dict[str, Any]:
    """Parse REMOTE_PROBE stdout into collect_local_facts-shaped dict."""
    chunks = {"LSBLK": "", "ROOT": "", "ZIMBRA": "", "ZIMBRA_DIR": ""}
    current = ""
    for line in (text or "").splitlines():
        if line.startswith("---") and line.endswith("---"):
            current = line.strip("-")
            continue
        if current in chunks:
            chunks[current] += line + "\n"
    lsblk: dict[str, Any] = {}
    raw = chunks["LSBLK"].strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                lsblk = parsed
        except json.JSONDecodeError:
            lsblk = {}
    zimbra_dir = chunks["ZIMBRA_DIR"].strip().lower() == "yes"
    return {
        "lsblk": lsblk,
        "lsblk_ok": bool(lsblk),
        "root_source": _norm_dev(chunks["ROOT"]),
        "zimbra_source": _norm_dev(chunks["ZIMBRA"]),
        "zimbra_exists": zimbra_dir,
    }


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return int(proc.returncode or 0), (proc.stdout or "").strip()


def collect_local_facts() -> dict[str, Any]:
    code, out = _run(
        [
            "lsblk",
            "-J",
            "-b",
            "-o",
            "NAME,PATH,TYPE,SIZE,FSTYPE,MOUNTPOINT,PKNAME,PTTYPE",
        ]
    )
    lsblk: dict[str, Any] = {}
    if code == 0 and out:
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                lsblk = parsed
        except json.JSONDecodeError:
            lsblk = {}
    _, root_src = _run(["findmnt", "-n", "-o", "SOURCE", "/"])
    zimbra_exists = os.path.isdir(os.environ.get("KIN_ZIMBRA_ROOT", "/opt/zimbra"))
    zimbra_src = ""
    if zimbra_exists:
        _, zimbra_src = _run(["findmnt", "-n", "-o", "SOURCE", "/opt/zimbra"])
    return {
        "lsblk": lsblk,
        "lsblk_ok": code == 0 and bool(lsblk),
        "root_source": _norm_dev(root_src),
        "zimbra_source": _norm_dev(zimbra_src),
        "zimbra_exists": zimbra_exists,
    }


def evaluate_facts(
    facts: dict[str, Any],
    *,
    label: str,
    require_zimbra_on_data: bool,
) -> dict[str, Any]:
    if not facts.get("lsblk_ok"):
        return {
            "ok": False,
            "label": label,
            "errors": [f"{label}: could not read lsblk JSON (lsblk -J)."],
            "seen": [],
        }
    return evaluate_node(
        lsblk=facts.get("lsblk") or {},
        root_source=str(facts.get("root_source") or ""),
        zimbra_source=str(facts.get("zimbra_source") or ""),
        zimbra_exists=bool(facts.get("zimbra_exists")),
        require_zimbra_on_data=require_zimbra_on_data,
        data_disk=data_disk_path(),
        meta_disk=meta_disk_path(),
        label=label,
    )
