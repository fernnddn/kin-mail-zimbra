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
import { HA_TOPOLOGY_OFFERED } from "../../featureFlags";
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
    if (!HA_TOPOLOGY_OFFERED && draft.topology !== "1vm") {
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
          : "Two-server high availability is not offered in this release. It is planned, and an appliance deployed now does not have to be rebuilt for it."}
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
