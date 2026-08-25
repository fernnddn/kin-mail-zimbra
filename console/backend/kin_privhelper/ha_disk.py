"""DRBD backing-disk preflight + fail-closed auto-partition plan.

The console preflight is still read-only. Partitioning happens only in the
Ansible drbd_disk_prep role, and only when plan_auto_partition() selects
exactly one blank spare disk.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

# Proven HA layout (HA-RUNBOOK): data + small external meta.
DEFAULT_DATA_DISK = "/dev/sdb1"
DEFAULT_META_DISK = "/dev/sdb2"
LUKS_MAPPER_NAME = "kin-zimbra-crypt"
LUKS_MAPPER_PATH = "/dev/mapper/kin-zimbra-crypt"
MIN_DATA_BYTES = 20 * 1024**3  # 20 GiB floor - not a sizing recommendation
MIN_META_BYTES = 128 * 1024**2
MAX_META_BYTES = 2 * 1024**3

_DEV_RE = re.compile(r"^/dev/[a-zA-Z0-9/._+-]+$")


def _selector_paths() -> list[Path]:
    here = Path(__file__).resolve()
    paths = [
        here.parents[3] / "ansible/roles/drbd_disk_prep/files/select_drbd_disk.py",
    ]
    env = os.environ.get("KIN_ANSIBLE_DIR", "").strip()
    if env:
        paths.append(Path(env) / "roles/drbd_disk_prep/files/select_drbd_disk.py")
    paths.append(
        Path("/opt/kin-mail-console/ansible/roles/drbd_disk_prep/files/select_drbd_disk.py")
    )
    return paths


def _load_selector() -> Any:
    for path in _selector_paths():
        if not path.is_file():
            continue
        spec = importlib.util.spec_from_file_location("kin_select_drbd_disk", path)
        if spec is None or spec.loader is None:
            continue
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    raise RuntimeError(
        "select_drbd_disk.py missing - re-run console/bootstrap.sh so ansible/ is installed"
    )


def plan_auto_partition(
    lsblk: dict[str, Any],
    *,
    root_source: str,
    data_disk: str = DEFAULT_DATA_DISK,
    meta_disk: str = DEFAULT_META_DISK,
) -> dict[str, Any]:
    """Return skip / partition / fail. Never writes a partition table."""
    return _load_selector().plan_auto_partition(
        lsblk,
        root_source=root_source,
        data_disk=data_disk,
        meta_disk=meta_disk,
    )


def install_prepare_plan(
    lsblk: dict[str, Any],
    *,
    root_source: str,
    data_disk: str = DEFAULT_DATA_DISK,
    meta_disk: str = DEFAULT_META_DISK,
) -> dict[str, Any]:
    """Return install_mode prepare_data or os_root. Never writes a partition table."""
    return _load_selector().install_prepare_plan(
        lsblk,
        root_source=root_source,
        data_disk=data_disk,
        meta_disk=meta_disk,
    )


def data_disk_path() -> str:
    return str(os.environ.get("KIN_DRBD_DATA_DISK") or DEFAULT_DATA_DISK).strip() or DEFAULT_DATA_DISK


def meta_disk_path() -> str:
    return str(os.environ.get("KIN_DRBD_META_DISK") or DEFAULT_META_DISK).strip() or DEFAULT_META_DISK


def luks_mapper_path() -> str:
    name = str(os.environ.get("KIN_LUKS_MAPPER_NAME") or LUKS_MAPPER_NAME).strip() or LUKS_MAPPER_NAME
    if name.startswith("/dev/"):
        return name
    return f"/dev/mapper/{name}"


def encrypt_zimbra_data_disk_enabled(value: str | None = None) -> bool:
    raw = (value if value is not None else os.environ.get("KIN_ENCRYPT_ZIMBRA_DATA_DISK", "1")).strip()
    return raw not in ("0", "false", "FALSE", "no", "NO", "off", "OFF")


def luks_prepare_action(*, encrypt: bool, fstype: str) -> str:
    """Mirror install/lib/zimbra-data-disk-luks.sh luks_prepare_action.

    format_luks is only for an empty disk. crypto_LUKS never reformats.
    Existing ext4 stays plain (no live-pair retrofit).
    """
    kind = (fstype or "").strip()
    if kind == "crypto_LUKS":
        return "open_luks"
    if kind == "ext4":
        return "skip_plain_existing"
    if kind == "":
        return "format_luks" if encrypt else "mkfs_plain"
    return "die_unknown"


def drbd_backing_device(*, data_disk: str, fstype: str, encrypt: bool | None = None) -> str:
    """Device DRBD should attach (mapper after LUKS, otherwise the raw partition)."""
    enabled = encrypt_zimbra_data_disk_enabled() if encrypt is None else encrypt
    action = luks_prepare_action(encrypt=enabled, fstype=fstype)
    if action in ("format_luks", "open_luks"):
        return luks_mapper_path()
    return data_disk or DEFAULT_DATA_DISK


def _norm_dev(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    # findmnt -n SOURCE can print the mapper twice (bind over LUKS). Taking
    # the whole blob made "Zimbra is on mapper\nmapper, not /dev/sdb1".
    text = text.splitlines()[0].strip()
    # findmnt SOURCE may also be "/dev/sda2[ /]" or "/dev/mapper/foo[/opt/zimbra]".
    if text.startswith("/dev/"):
        return text.split("[", 1)[0].split(" ", 1)[0]
    return text


def _source_is_on_data_disk(
    lsblk: dict[str, Any], *, source: str, data_disk: str
) -> bool:
    """True when findmnt SOURCE is the data partition or a child (LUKS crypt)."""
    src = _norm_dev(source)
    data = _norm_dev(data_disk)
    if not src or not data:
        return False
    src_uuid = ""
    if src.upper().startswith("UUID="):
        src_uuid = src.split("=", 1)[1].strip().lower()

    def walk(devices: list[Any], under_data: bool) -> bool:
        for dev in devices:
            if not isinstance(dev, dict):
                continue
            path = _norm_dev(_dev_path(dev))
            here = under_data or path == data
            node_uuid = str(dev.get("uuid") or "").strip().lower()
            if here and path == src:
                return True
            if here and src_uuid and node_uuid == src_uuid:
                return True
            kids = dev.get("children")
            if isinstance(kids, list) and walk(kids, here):
                return True
        return False

    block = lsblk.get("blockdevices") if isinstance(lsblk, dict) else None
    if not isinstance(block, list):
        return False
    return walk(block, False)


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


def _data_disk_unmounted_with_fs(
    nodes: dict[str, dict[str, Any]], data_disk: str
) -> bool:
    """True when the data partition has a filesystem but is not mounted.

    Used to recognise the mid-handoff state after release-zimbra-plain-mount-for-drbd.sh:
    Zimbra data still lives on the partition, /opt/zimbra is only an empty mountpoint.
    """
    node = nodes.get(data_disk)
    if node is None:
        return False
    fstype = str(node.get("fstype") or "").strip()
    if not fstype:
        return False
    mountpoint = str(node.get("mountpoint") or "").strip()
    return mountpoint == ""


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
    zimbra_tree_present: bool | None = None,
) -> dict[str, Any]:
    """Return a JSON-serialisable result. ok=False is fail-closed.

    zimbra_tree_present: whether bin/zmcontrol is visible under /opt/zimbra on the
    currently mounted directory tree. None means "unknown" (legacy callers) and
    keeps the fail-closed behaviour when findmnt returns empty.
    """
    data_disk = _norm_dev(data_disk) or DEFAULT_DATA_DISK
    meta_disk = _norm_dev(meta_disk) or DEFAULT_META_DISK
    if not _DEV_RE.match(data_disk) or not _DEV_RE.match(meta_disk):
        return {
            "ok": False,
            "label": label,
            "errors": ["internal: DRBD disk path is not a /dev/ device"],
            "seen": [],
            "can_auto_partition": False,
            "will_auto_partition": None,
            "plan_action": "fail",
            "build_allowed": False,
        }

    nodes = {_dev_path(n): n for n in flatten_lsblk(lsblk) if _dev_path(n)}
    seen = summarize_disks(lsblk, root_source=root_source)
    errors: list[str] = []
    plan = plan_auto_partition(
        lsblk, root_source=root_source, data_disk=data_disk, meta_disk=meta_disk
    )
    can_auto = plan.get("action") == "partition"
    will_auto = (
        {
            "label": label,
            "disk": plan.get("disk"),
            "data_disk": plan.get("data_disk"),
            "meta_disk": plan.get("meta_disk"),
            "size_human": plan.get("size_human"),
            "message": plan.get("message"),
        }
        if can_auto
        else None
    )
    plan_data = _norm_dev(str(plan.get("data_disk") or ""))
    plan_meta = _norm_dev(str(plan.get("meta_disk") or ""))
    if str(plan.get("action") or "") in ("partition", "skip") and plan_data and plan_meta:
        # Selector may pick vdb/nvme while callers still default to sdb1.
        # Existence checks and combine_results must use the planned paths.
        data_disk = plan_data
        meta_disk = plan_meta

    data_parent = f"/dev/{_parent_disk_name(data_disk.removeprefix('/dev/'))}"
    data_node = nodes.get(data_disk)
    parent_node = nodes.get(data_parent)

    if data_node is None:
        if can_auto:
            pass
        elif plan.get("action") == "fail":
            for err in plan.get("errors") or []:
                errors.append(f"{label}: {err}")
        elif parent_node is not None and str(parent_node.get("type") or "") == "disk":
            errors.append(
                f"{label}: second disk not partitioned - found {data_parent} but not {data_disk}. "
                f"Create GPT partitions {data_disk} (Zimbra/DRBD data, ≥{_fmt_bytes(MIN_DATA_BYTES)}) "
                f"and {meta_disk} (~256 MiB DRBD meta). Do not mkfs the meta partition."
            )
        else:
            extra = f" Seen: {'; '.join(seen)}." if seen else " No extra disk was visible to lsblk."
            errors.append(
                f"{label}: second disk not found - expected DRBD data partition {data_disk}. "
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
                f"{label}: {data_disk} is on the OS disk - refusing to use it for DRBD."
            )

    meta_node = nodes.get(meta_disk)
    if meta_node is None:
        if can_auto:
            pass
        elif data_node is not None or parent_node is not None:
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
                f"{label}: {meta_disk} is mounted on {mp} - meta must stay unformatted/unmounted."
            )

    zimbra_src = _norm_dev(zimbra_source)
    zimbra_mid_handoff = False
    if require_zimbra_on_data and zimbra_exists:
        allowed = {
            data_disk,
            luks_mapper_path(),
            "/dev/drbd0",
            "/dev/drbd/by-res/kin-zimbra",
        }
        if zimbra_src.startswith("/dev/drbd"):
            pass
        elif zimbra_src in allowed:
            pass
        elif _source_is_on_data_disk(
            lsblk, source=zimbra_src, data_disk=data_disk
        ):
            pass
        elif (
            not zimbra_src
            and zimbra_tree_present is False
            and _data_disk_unmounted_with_fs(nodes, data_disk)
        ):
            # Mid-handoff after release-zimbra-plain-mount-for-drbd.sh: findmnt is
            # empty and /opt/zimbra has no visible zmcontrol because the real tree
            # sits on the unmounted data partition (which still has a filesystem).
            # Never-migrated hosts still have bin/zmcontrol on the OS volume.
            zimbra_mid_handoff = True
        else:
            where = zimbra_src or "the root volume"
            errors.append(
                f"{label}: Zimbra is on {where}, not {data_disk}. Build HA pair would replicate "
                "an empty DRBD disk and leave mail on the OS volume. Move /opt/zimbra onto "
                f"{data_disk} first (stop Zimbra, rsync, fstab UUID, start - see HA-RUNBOOK "
                "§13 / install/lib/migrate-zimbra-to-drbd-disk.sh)."
            )

    ok = not errors and not can_auto
    return {
        "ok": ok,
        "label": label,
        "data_disk": data_disk,
        "meta_disk": meta_disk,
        "root_source": _norm_dev(root_source),
        "zimbra_source": zimbra_src,
        "zimbra_exists": zimbra_exists,
        "zimbra_tree_present": zimbra_tree_present,
        "zimbra_mid_handoff": zimbra_mid_handoff,
        "seen": seen,
        "errors": errors,
        "can_auto_partition": can_auto,
        "will_auto_partition": will_auto,
        "plan_action": str(plan.get("action") or "fail"),
        # Per node: False once GPT exists but Zimbra is still on the OS volume.
        # Pair-level allow lives in combine_results (a sibling may still need GPT).
        "build_allowed": (not errors) or can_auto,
    }


def _node_blocks_pair_prep(node: dict[str, Any]) -> bool:
    """True if this node must stop Build HA even while a sibling still needs GPT.

    Skip-layout (already partitioned) plus Zimbra-still-on-root must not veto
    remaining disk_prep. Inspect failures and layout fail-closed plans must.
    """
    if node.get("ok") or node.get("can_auto_partition"):
        return False
    if str(node.get("plan_action") or "") == "skip":
        return False
    return True


def combine_results(*nodes: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    will: list[dict[str, Any]] = []
    for node in nodes:
        errors.extend(str(e) for e in (node.get("errors") or []))
        auto = node.get("will_auto_partition")
        if isinstance(auto, dict) and auto.get("disk"):
            will.append(auto)
    ok = all(bool(n.get("ok")) for n in nodes) if nodes else False
    # Pair-level allow: every node is ready for DRBD, OR at least one host still
    # needs GPT and no host is an inspect/layout hard-fail.
    # Do not use all(per-node build_allowed): a finished GPT host with Zimbra
    # still on the OS volume has build_allowed False, and that must not block
    # disk_prep on a blank peer. After every disk is GPT and Zimbra is still
    # on root, `will` is empty and this becomes False so DRBD cannot start.
    # Orchestration also re-checks combined `ok` after disk_prep before DRBD.
    pending = bool(will)
    blocked = any(_node_blocks_pair_prep(n) for n in nodes) if nodes else True
    build_allowed = ok or (pending and not blocked)
    uniq_data = {
        str(n.get("data_disk") or "").strip()
        for n in nodes
        if str(n.get("data_disk") or "").strip()
    }
    uniq_meta = {
        str(n.get("meta_disk") or "").strip()
        for n in nodes
        if str(n.get("meta_disk") or "").strip()
    }
    for item in will:
        d = str(item.get("data_disk") or "").strip()
        m = str(item.get("meta_disk") or "").strip()
        if d:
            uniq_data.add(d)
        if m:
            uniq_meta.add(m)
    if len(uniq_data) > 1:
        errors.append(
            "mail nodes would use different DRBD data disks ("
            + ", ".join(sorted(uniq_data))
            + "). Both VMs need the same device path."
        )
        build_allowed = False
    data_disk = next(iter(uniq_data)) if len(uniq_data) == 1 else ""
    meta_disk = next(iter(uniq_meta)) if len(uniq_meta) == 1 else ""
    if will:
        instructions = (
            "Build HA pair will GPT-partition the unique blank spare disk on each "
            "server that still needs it (not the OS disk; no existing table). "
            "It then re-checks before DRBD. If /opt/zimbra is still on the root "
            "volume after that, it stops - migrate onto the data partition first "
            "(HA-RUNBOOK §13 / install/lib/migrate-zimbra-to-drbd-disk.sh)."
        )
    else:
        instructions = (
            "On each mail VM: attach exactly one unused blank disk ≥20 GiB (not the "
            f"OS disk). Build HA pair will GPT-partition {data_disk_path()} (data) and "
            f"{meta_disk_path()} (~256 MiB meta, no mkfs) when that disk is unambiguous. "
            "Zero or multiple spare disks stay fail-closed. If Zimbra is already on "
            "the root volume, migrate /opt/zimbra onto the data partition before DRBD "
            "(HA-RUNBOOK §13 / install/lib/migrate-zimbra-to-drbd-disk.sh)."
        )
    return {
        "ok": ok,
        "build_allowed": build_allowed,
        "nodes": list(nodes),
        "errors": errors,
        "will_auto_partition": will,
        "instructions": instructions,
        "data_disk": data_disk,
        "meta_disk": meta_disk,
    }


REMOTE_PROBE = (
    "echo '---LSBLK---'; "
    "lsblk -J -b -o NAME,PATH,TYPE,SIZE,FSTYPE,MOUNTPOINT,PKNAME,PTTYPE,UUID; "
    "echo '---ROOT---'; "
    "findmnt -n -o SOURCE /; "
    "echo '---ZIMBRA---'; "
    "findmnt -n -o SOURCE /opt/zimbra 2>/dev/null || true; "
    "echo '---ZIMBRA_DIR---'; "
    "if [ -d /opt/zimbra ]; then echo yes; else echo no; fi; "
    "echo '---ZIMBRA_TREE---'; "
    "if [ -x /opt/zimbra/bin/zmcontrol ]; then echo yes; else echo no; fi"
)


def parse_remote_probe(text: str) -> dict[str, Any]:
    """Parse REMOTE_PROBE stdout into collect_local_facts-shaped dict."""
    chunks = {
        "LSBLK": "",
        "ROOT": "",
        "ZIMBRA": "",
        "ZIMBRA_DIR": "",
        "ZIMBRA_TREE": "",
    }
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
    tree_chunk = chunks["ZIMBRA_TREE"].strip().lower()
    # Older peers without the ZIMBRA_TREE section leave this empty -> None.
    zimbra_tree_present: bool | None
    if tree_chunk == "yes":
        zimbra_tree_present = True
    elif tree_chunk == "no":
        zimbra_tree_present = False
    else:
        zimbra_tree_present = None
    return {
        "lsblk": lsblk,
        "lsblk_ok": bool(lsblk),
        "root_source": _norm_dev(chunks["ROOT"]),
        "zimbra_source": _norm_dev(chunks["ZIMBRA"]),
        "zimbra_exists": zimbra_dir,
        "zimbra_tree_present": zimbra_tree_present,
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
            "NAME,PATH,TYPE,SIZE,FSTYPE,MOUNTPOINT,PKNAME,PTTYPE,UUID",
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
    zimbra_root = os.environ.get("KIN_ZIMBRA_ROOT", "/opt/zimbra")
    zimbra_exists = os.path.isdir(zimbra_root)
    zimbra_src = ""
    zimbra_tree_present = False
    if zimbra_exists:
        _, zimbra_src = _run(["findmnt", "-n", "-o", "SOURCE", "/opt/zimbra"])
        zimbra_tree_present = os.path.isfile(
            os.path.join(zimbra_root, "bin", "zmcontrol")
        )
    return {
        "lsblk": lsblk,
        "lsblk_ok": code == 0 and bool(lsblk),
        "root_source": _norm_dev(root_src),
        "zimbra_source": _norm_dev(zimbra_src),
        "zimbra_exists": zimbra_exists,
        "zimbra_tree_present": zimbra_tree_present,
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
            "can_auto_partition": False,
            "will_auto_partition": None,
            "plan_action": "fail",
            "build_allowed": False,
        }
    tree_raw = facts.get("zimbra_tree_present")
    tree_present: bool | None
    if tree_raw is None:
        tree_present = None
    else:
        tree_present = bool(tree_raw)
    return evaluate_node(
        lsblk=facts.get("lsblk") or {},
        root_source=str(facts.get("root_source") or ""),
        zimbra_source=str(facts.get("zimbra_source") or ""),
        zimbra_exists=bool(facts.get("zimbra_exists")),
        require_zimbra_on_data=require_zimbra_on_data,
        data_disk=data_disk_path(),
        meta_disk=meta_disk_path(),
        label=label,
        zimbra_tree_present=tree_present,
    )
