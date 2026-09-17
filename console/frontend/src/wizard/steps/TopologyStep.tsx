import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Button,
  Choice,
  ChoiceGrid,
  Err,
  FieldLabel,
  FieldRow,
  Hint,
  Input,
  Lede,
  NavRow,
  Title,
} from "../../ui";
import { HA_TOPOLOGY_OFFERED, SPLIT_TOPOLOGY_OFFERED } from "../../featureFlags";
import { useWizard } from "../WizardContext";
import type { WizardDraft } from "../types";

const IPV4 =
  /^(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}$/;

function validIpv4(value: string): boolean {
  return IPV4.test(value.trim());
}

export default function TopologyStep() {
  const { draft, setLocal, save, error, saving } = useWizard();
  const navigate = useNavigate();
  const [fieldErr, setFieldErr] = useState("");

  /* With one layout on offer, choosing it is not a decision, so the wizard
     makes it. Leaving the step with nothing selected would refuse to continue
     over a choice the operator was never shown. */
  useEffect(() => {
    /* Only auto-pick when there is genuinely nothing to choose. With the split
       layout on offer this is a real decision, and pre-selecting one would
       quietly overwrite an operator who had already picked the other. */
    if (SPLIT_TOPOLOGY_OFFERED || HA_TOPOLOGY_OFFERED) return;
    if (draft.topology !== "1vm") {
      setLocal({
        topology: "1vm",
        peer_host_ip: "",
        peer_host_name: "",
        observability_vm_ip: "",
        cluster_vip_ip: "",
      });
    }
  }, [draft.topology, setLocal]);

  async function pick(topology: WizardDraft["topology"]) {
    setFieldErr("");
    if (topology === "1vm") {
      setLocal({ topology, peer_host_ip: "", peer_host_name: "", observability_vm_ip: "", cluster_vip_ip: "" });
    } else if (topology === "split") {
      /* The edge is the machine running this wizard, so it is known already and
         is shown rather than asked for. Only the mailbox is a real question. */
      setLocal({
        topology,
        peer_host_ip: "",
        peer_host_name: "",
        observability_vm_ip: "",
        cluster_vip_ip: "",
        edge_ip: draft.edge_ip || draft.local_host_ip || "",
        edge_host: draft.edge_host || draft.mail_host || "",
      });
    } else {
      setLocal({ topology });
    }
  }

  async function next() {
    if (!draft.topology) {
      setFieldErr(
        HA_TOPOLOGY_OFFERED
          ? "Choose 1 server or 2 servers before continuing."
          : "Select the single server layout before continuing.",
      );
      return;
    }
    if (draft.topology === "2vm" && !draft.peer_host_ip.trim()) {
      setFieldErr("Enter the second server IP to continue with a 2-server layout.");
      return;
    }
    if (draft.topology === "2vm" && !validIpv4(draft.peer_host_ip)) {
      setFieldErr("Second server IP must be an IPv4 address.");
      return;
    }
    if (draft.topology === "2vm" && !draft.observability_vm_ip.trim()) {
      setFieldErr("Enter the Observability VM IP (qdevice witness) to continue.");
      return;
    }
    if (draft.topology === "2vm" && !validIpv4(draft.observability_vm_ip)) {
      setFieldErr("Observability VM IP must be an IPv4 address.");
      return;
    }
    if (draft.topology === "2vm" && !draft.cluster_vip_ip.trim()) {
      setFieldErr("Enter the cluster VIP (unused IPv4, not a mail-node address).");
      return;
    }
    if (draft.topology === "2vm" && !validIpv4(draft.cluster_vip_ip)) {
      setFieldErr("Cluster VIP must be an IPv4 address.");
      return;
    }
    if (draft.topology === "2vm") {
      const vip = draft.cluster_vip_ip.trim();
      const peer = draft.peer_host_ip.trim();
      const obs = draft.observability_vm_ip.trim();
      const local = (draft.local_host_ip || "").trim();
      if (vip === peer) {
        setFieldErr("Cluster VIP must not be the second server IP.");
        return;
      }
      if (vip === obs) {
        setFieldErr("Cluster VIP must not be the Observability VM IP.");
        return;
      }
      if (local && vip === local) {
        setFieldErr("Cluster VIP must not be this server's own address.");
        return;
      }
    }
    if (draft.topology === "split") {
      const edgeIp = (draft.edge_ip || draft.local_host_ip || "").trim();
      const mboxIp = (draft.mailbox_ip || "").trim();
      const edgeHost = (draft.edge_host || draft.mail_host || "").trim();
      const mboxHost = (draft.mailbox_host || "").trim();
      if (!mboxIp) {
        setFieldErr("Enter the mailbox server's IP address.");
        return;
      }
      if (!validIpv4(mboxIp)) {
        setFieldErr("The mailbox IP must be an IPv4 address.");
        return;
      }
      if (edgeIp && mboxIp === edgeIp) {
        setFieldErr("The mailbox must be a different machine from this one.");
        return;
      }
      if (!mboxHost) {
        setFieldErr("Enter a hostname for the mailbox server.");
        return;
      }
      if (edgeHost && mboxHost === edgeHost) {
        setFieldErr("The mailbox needs its own hostname, different from this server's.");
        return;
      }
      if (!draft.mailbox_ssh_pass && !draft.mailbox_ssh_pass_set) {
        setFieldErr(
          "Enter the password for the mailbox account. This server builds the mailbox over SSH, so you never log into it by hand.",
        );
        return;
      }
      setFieldErr("");
      await save({
        topology: "split",
        edge_ip: edgeIp,
        edge_host: edgeHost,
        mailbox_ip: mboxIp,
        mailbox_host: mboxHost,
        mailbox_ssh_user: (draft.mailbox_ssh_user || "kin").trim(),
        mailbox_ssh_pass: draft.mailbox_ssh_pass || "",
        peer_host_ip: "",
        peer_host_name: "",
        observability_vm_ip: "",
        cluster_vip_ip: "",
        current_step: "credentials",
      });
      navigate("/wizard/credentials");
      return;
    }
    setFieldErr("");
    const peer_host_ip = draft.topology === "2vm" ? draft.peer_host_ip.trim() : "";
    const peer_host_name = draft.topology === "2vm" ? draft.peer_host_name.trim() : "";
    const observability_vm_ip = draft.topology === "2vm" ? draft.observability_vm_ip.trim() : "";
    const cluster_vip_ip = draft.topology === "2vm" ? draft.cluster_vip_ip.trim() : "";
    await save({
      topology: draft.topology,
      peer_host_ip,
      peer_host_name,
      observability_vm_ip,
      cluster_vip_ip,
      current_step: "credentials",
    });
    navigate("/wizard/credentials");
  }

  return (
    <>
      <Title>How many servers?</Title>
      <Lede>
        {HA_TOPOLOGY_OFFERED
          ? "Pick the layout for this customer. One server is the standard KIN Mail deployment and the layout this release is built around. Two servers add active-passive high availability and remain available on request, but they are not the default."
          : SPLIT_TOPOLOGY_OFFERED
            ? "One machine can run every Zimbra role, or the roles can be split across two so that the server facing the internet holds no mail. Both are built from this appliance."
            : "This release deploys a single mail server. That is the layout KIN Mail is hardened and tuned for, and the one this appliance will run."}
      </Lede>
      <ChoiceGrid>
        <Choice
          type="button"
          selected={draft.topology === "1vm"}
          onClick={() => void pick("1vm")}
        >
          <strong>1 server (recommended)</strong>
          <span>
            Single-node install, with built-in monitoring and reporting from the first deploy. The
            right choice when the virtualization platform already provides host-level redundancy.
          </span>
        </Choice>
        {SPLIT_TOPOLOGY_OFFERED ? (
        <Choice
          type="button"
          selected={draft.topology === "split"}
          onClick={() => void pick("split")}
        >
          <strong>2 servers, split roles</strong>
          <span>
            Mail delivery and the webmail proxy run here; the directory and every mailbox live on a
            second machine behind it. The server facing the internet holds no mail, so losing it
            costs a rebuild rather than data. Both machines are built from this one - you never log
            into the second.
          </span>
        </Choice>
        ) : null}
        {HA_TOPOLOGY_OFFERED ? (
        <Choice
          type="button"
          selected={draft.topology === "2vm"}
          onClick={() => void pick("2vm")}
        >
          <strong>2 servers (optional)</strong>
          <span>
            Active-passive high availability with live data replication and automatic failover.
            Needs a second mail server plus a separate Observability VM, and it is not the layout
            this release is tuned for. Choose it only when high availability has been agreed for
            this customer.
          </span>
        </Choice>
        ) : null}
      </ChoiceGrid>
      {draft.topology === "split" ? (
        <>
          <Hint>
            This server is the edge: {(draft.edge_ip || draft.local_host_ip) || "this machine"}
            {draft.mail_host ? ` (${draft.mail_host})` : ""}. Everything below describes the second
            machine, which must be a fresh Ubuntu with an account that can sudo.
          </Hint>
          <FieldRow>
            <FieldLabel htmlFor="mailbox_ip" required>
              Mailbox server IP
            </FieldLabel>
            <Input
              id="mailbox_ip"
              value={draft.mailbox_ip}
              onChange={(e) => {
                setFieldErr("");
                setLocal({ mailbox_ip: e.target.value });
              }}
              placeholder="192.0.2.4"
              autoComplete="off"
            />
            <Hint>Where the directory and the mail will live. Reached from here over SSH.</Hint>
          </FieldRow>
          <FieldRow>
            <FieldLabel htmlFor="mailbox_host" required>
              Mailbox server hostname
            </FieldLabel>
            <Input
              id="mailbox_host"
              value={draft.mailbox_host}
              onChange={(e) => {
                setFieldErr("");
                setLocal({ mailbox_host: e.target.value });
              }}
              placeholder="store.example.co.id"
              autoComplete="off"
            />
            <Hint>
              Its own name, not the one users type. Zimbra keys every server entry on this, so it
              has to differ from this machine's.
            </Hint>
          </FieldRow>
          <FieldRow>
            <FieldLabel htmlFor="mailbox_ssh_user" required>
              Account on the mailbox
            </FieldLabel>
            <Input
              id="mailbox_ssh_user"
              value={draft.mailbox_ssh_user || "kin"}
              onChange={(e) => {
                setFieldErr("");
                setLocal({ mailbox_ssh_user: e.target.value });
              }}
              placeholder="kin"
              autoComplete="off"
            />
          </FieldRow>
          <FieldRow>
            <FieldLabel htmlFor="mailbox_ssh_pass" required={!draft.mailbox_ssh_pass_set}>
              Password for that account
            </FieldLabel>
            <Input
              id="mailbox_ssh_pass"
              type="password"
              value={draft.mailbox_ssh_pass}
              onChange={(e) => {
                setFieldErr("");
                setLocal({ mailbox_ssh_pass: e.target.value });
              }}
              placeholder={draft.mailbox_ssh_pass_set ? "unchanged" : ""}
              autoComplete="new-password"
            />
            <Hint>
              {draft.mailbox_ssh_pass_set
                ? "A password is already stored. Leave this blank to keep it."
                : "Used once, to build the mailbox from here. Stored on this appliance only and never shown again."}
            </Hint>
          </FieldRow>
          <Hint>
            The mailbox needs one blank, unpartitioned spare disk for the mail. A disk that already
            has a partition table is left alone, and Zimbra falls back to the system volume.
          </Hint>
        </>
      ) : null}
      {draft.topology === "2vm" ? (
        <>
          <FieldRow>
            <FieldLabel htmlFor="peer_host_ip" required>
              Second server IP
            </FieldLabel>
            <Input
              id="peer_host_ip"
              value={draft.peer_host_ip}
              onChange={(e) => {
                setFieldErr("");
                setLocal({ peer_host_ip: e.target.value });
              }}
              placeholder="192.0.2.14"
              autoComplete="off"
            />
            <Hint>
              IP of the peer host for this HA pair. Connectivity is not checked here; that comes
              later during orchestration.
            </Hint>
          </FieldRow>
          <FieldRow>
            <FieldLabel htmlFor="peer_host_name" optional>
              Second server hostname
            </FieldLabel>
            <Input
              id="peer_host_name"
              value={draft.peer_host_name}
              onChange={(e) => setLocal({ peer_host_name: e.target.value })}
              placeholder="mail2.example.co.id"
              autoComplete="off"
            />
          </FieldRow>
          <FieldRow>
            <FieldLabel htmlFor="observability_vm_ip" required>
              Observability VM IP
            </FieldLabel>
            <Input
              id="observability_vm_ip"
              value={draft.observability_vm_ip}
              onChange={(e) => {
                setFieldErr("");
                setLocal({ observability_vm_ip: e.target.value });
              }}
              placeholder="192.0.2.12"
              autoComplete="off"
            />
            <Hint>
              Independent witness for quorum (qdevice) plus monitoring. Same step as the second
              mail server because a 2-server HA pair is not complete without it.
            </Hint>
          </FieldRow>
          <FieldRow>
            <FieldLabel htmlFor="cluster_vip_ip" required>
              Cluster VIP
            </FieldLabel>
            <Input
              id="cluster_vip_ip"
              value={draft.cluster_vip_ip}
              onChange={(e) => {
                setFieldErr("");
                setLocal({ cluster_vip_ip: e.target.value });
              }}
              placeholder="192.0.2.16"
              autoComplete="off"
            />
            <Hint>
              Unused IPv4 that Pacemaker will float to whichever node is Promoted. Must not be this
              server, the second server, or the Observability VM.
              {draft.local_host_ip ? ` This server is ${draft.local_host_ip}.` : ""}
            </Hint>
          </FieldRow>
          <Hint>
            Each mail VM needs exactly one unused spare disk (≥20 GiB, not the OS disk, no
            partition table, not formatted by cloud-init). SCSI/SATA (sdb), virtio (vdb), and
            NVMe are all accepted. Build HA pair GPT-partitions that disk: partition 1 = Zimbra
            data, partition 2 ≈ 256 MiB DRBD meta (no filesystem). Zero or two spare disks stay
            fail-closed.
          </Hint>
          <Hint>
            Build HA installs the admin console on Mail B from this host (no second
            git clone or console/bootstrap.sh on B). Mail B still needs user kin,
            matching passwords, SSH password login, and one blank spare disk. After
            bootstrap on this host, the git checkout under home is optional to keep;
            runtime lives in /opt/kin-mail-console.
          </Hint>
        </>
      ) : null}
      <Hint>
        {HA_TOPOLOGY_OFFERED
          ? "Larger topologies are not offered in this wizard yet."
          : "Active-passive high availability - two servers replicating the same mail, with automatic failover - is a different layout and is not offered in this release."}
      </Hint>
      <Err>{fieldErr || error}</Err>
      <NavRow>
        <span />
        <Button type="button" loading={saving} onClick={() => void next()}>
          Continue
        </Button>
      </NavRow>
    </>
  );
}
