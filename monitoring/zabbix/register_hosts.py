#!/usr/bin/env python3
"""Create KIN Mail HA/Backup Zabbix 6.0 templates and register hosts.

Env: ZABBIX_URL (default http://10.10.40.12:8080/api_jsonrpc.php)
     ZABBIX_USER (default Admin)
     ZABBIX_PASSWORD (required)
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

URL = os.environ.get("ZABBIX_URL", "http://10.10.40.12:8080/api_jsonrpc.php")
USER = os.environ.get("ZABBIX_USER", "Admin")
PASSWORD = os.environ["ZABBIX_PASSWORD"]

HOSTS = [
    {
        "host": "obser",
        "name": "Observability",
        "ip": "10.10.40.12",
        "templates": ["linux"],
    },
    {
        "host": "mail.gits-it.site",
        "name": "Mail A",
        "ip": "10.10.40.15",
        "templates": ["linux", "ha"],
    },
    {
        "host": "mail2.gits-it.site",
        "name": "Mail B",
        "ip": "10.10.40.14",
        "templates": ["linux", "ha"],
    },
    {
        "host": "backup.gits-it.site",
        "name": "Backup",
        "ip": "10.10.40.13",
        "templates": ["linux", "backup"],
    },
]


def api(method: str, params, auth=None, reqid=1):
    body = {"jsonrpc": "2.0", "method": method, "params": params, "id": reqid}
    if auth is not None:
        body["auth"] = auth
    req = urllib.request.Request(
        URL,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json-rpc"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    if "error" in data:
        raise SystemExit(f"{method}: {data['error']}")
    return data["result"]


def ensure_template(auth, name, groupid):
    found = api("template.get", {"filter": {"host": name}, "output": ["templateid"]}, auth)
    if found:
        return found[0]["templateid"]
    created = api(
        "template.create",
        {"host": name, "groups": [{"groupid": groupid}]},
        auth,
    )
    return created["templateids"][0]


def ensure_item(auth, templateid, name, key, delay="60s"):
    found = api(
        "item.get",
        {"hostids": templateid, "filter": {"key_": key}, "output": ["itemid"]},
        auth,
    )
    if found:
        return found[0]["itemid"]
    created = api(
        "item.create",
        {
            "name": name,
            "key_": key,
            "hostid": templateid,
            "type": 0,
            "value_type": 3,
            "delay": delay,
            "history": "90d",
            "trends": "365d",
        },
        auth,
    )
    return created["itemids"][0]


def ensure_trigger(auth, description, expression, priority):
    found = api(
        "trigger.get",
        {"filter": {"description": description}, "output": ["triggerid"]},
        auth,
    )
    if found:
        return found[0]["triggerid"]
    created = api(
        "trigger.create",
        {
            "description": description,
            "expression": expression,
            "priority": priority,
            "manual_close": 1,
        },
        auth,
    )
    return created["triggerids"][0]


def main() -> int:
    auth = api("user.login", {"user": USER, "password": PASSWORD})
    groups = api("hostgroup.get", {"filter": {"name": ["Linux servers", "Templates/Operating systems", "Templates"]}, "output": ["groupid", "name"]}, auth)
    linux_group = next((g["groupid"] for g in groups if g["name"] == "Linux servers"), None)
    tpl_group = next((g["groupid"] for g in groups if "Operating systems" in g["name"] or g["name"] == "Templates"), None)
    if not linux_group:
        created = api("hostgroup.create", {"name": "Linux servers"}, auth)
        linux_group = created["groupids"][0]
    if not tpl_group:
        created = api("hostgroup.create", {"name": "Templates/Applications"}, auth)
        tpl_group = created["groupids"][0]

    linux_tpl = api("template.get", {"filter": {"host": ["Linux by Zabbix agent"]}, "output": ["templateid"]}, auth)
    if not linux_tpl:
        raise SystemExit("template 'Linux by Zabbix agent' not found")
    linux_id = linux_tpl[0]["templateid"]

    ha_id = ensure_template(auth, "KIN Mail HA", tpl_group)
    backup_id = ensure_template(auth, "KIN Mail Backup", tpl_group)
    ensure_item(auth, ha_id, "KIN DRBD replication OK", "kin.drbd.ok")
    ensure_item(auth, ha_id, "KIN qdevice votes", "kin.qdevice.votes")
    ensure_item(auth, ha_id, "KIN new STONITH events", "kin.stonith.new_events")
    ensure_item(auth, ha_id, "KIN active mailbox count", "kin.mailbox.active_count", delay="60s")
    ensure_item(auth, ha_id, "KIN contracted seats", "kin.mailbox.contracted_seats", delay="60s")
    ensure_item(auth, backup_id, "KIN backup age seconds", "kin.backup.age_seconds", delay="300s")
    ensure_trigger(auth, "KIN DRBD not Established/UpToDate", f"last(/KIN Mail HA/kin.drbd.ok)=0", 4)
    ensure_trigger(auth, "KIN qdevice votes missing", f"last(/KIN Mail HA/kin.qdevice.votes)<1", 4)
    ensure_trigger(auth, "KIN new STONITH fence event", f"last(/KIN Mail HA/kin.stonith.new_events)>0", 4)
    ensure_trigger(auth, "KIN backup older than 26h", f"last(/KIN Mail Backup/kin.backup.age_seconds)>93600", 2)

    tplmap = {"linux": linux_id, "ha": ha_id, "backup": backup_id}
    for spec in HOSTS:
        existing = api("host.get", {"filter": {"host": [spec["host"]]}, "output": ["hostid"]}, auth)
        templates = [{"templateid": tplmap[t]} for t in spec["templates"]]
        if existing:
            api(
                "host.update",
                {"hostid": existing[0]["hostid"], "templates": templates},
                auth,
            )
            print(f"updated {spec['host']}")
            continue
        api(
            "host.create",
            {
                "host": spec["host"],
                "name": spec["name"],
                "interfaces": [
                    {
                        "type": 1,
                        "main": 1,
                        "useip": 1,
                        "ip": spec["ip"],
                        "dns": "",
                        "port": "10050",
                    }
                ],
                "groups": [{"groupid": linux_group}],
                "templates": templates,
            },
            auth,
        )
        print(f"created {spec['host']}")
    api("user.logout", [], auth)
    return 0


if __name__ == "__main__":
    sys.exit(main())
