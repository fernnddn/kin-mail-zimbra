# KIN Mail Zabbix extras (Timeline 3.7–3.9)

Agent `UserParameter` keys and a Zabbix 6.0 template for the HA/backup stack.
The Zabbix Server / Grafana / agent **packages** are installed by
[kin-sight-monitoring](https://github.com/azana-nisaa/kin-sight-monitoring) v1.9.3,
not by this repo.

## Keys

| Key | Hosts | Meaning |
| --- | --- | --- |
| `kin.drbd.ok` | mail A/B | `1` if replication Established and both disks UpToDate |
| `kin.qdevice.votes` | mail A/B | Qdevice vote count (`corosync-quorumtool`) |
| `kin.stonith.new_events` | mail A/B | Count of `pcs stonith history` events completed after **2026-08-13 09:05:00** (excludes the morning fence-test) |
| `kin.backup.age_seconds` | Backup VM | Age of newest `/var/lib/kin-mail-backup/daily/*` directory |

Triggers: DRBD `=0` (High), qdevice votes `<1` (High), stonith new events `>0` (High), backup age `>93600` (26h, Warning).

## Deploy on a node

```bash
sudo install -d /usr/local/lib/kin-zabbix
sudo install -m 0755 monitoring/zabbix/scripts/*.sh /usr/local/lib/kin-zabbix/
sudo install -m 0644 monitoring/zabbix/userparameter_kin.conf /etc/zabbix/zabbix_agentd.d/kin.conf
sudo install -m 0440 monitoring/zabbix/sudoers.d/kin-zabbix /etc/sudoers.d/kin-zabbix
sudo visudo -cf /etc/sudoers.d/kin-zabbix
sudo systemctl restart zabbix-agent
```

Link template **KIN Mail HA** on mail nodes and **KIN Mail Backup** on the Backup VM
(plus **Linux by Zabbix agent** on every host).
