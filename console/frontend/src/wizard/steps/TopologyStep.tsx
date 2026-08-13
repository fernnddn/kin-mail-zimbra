import { useState } from "react";
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

  async function pick(topology: WizardDraft["topology"]) {
    setFieldErr("");
    if (topology === "1vm") {
      setLocal({ topology, peer_host_ip: "", peer_host_name: "", observability_vm_ip: "", current_step: "topology" });
    } else {
      setLocal({ topology, current_step: "topology" });
    }
  }

  async function next() {
    if (!draft.topology) {
      setFieldErr("Choose 1 server or 2 servers before continuing.");
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
    setFieldErr("");
    const peer_host_ip = draft.topology === "2vm" ? draft.peer_host_ip.trim() : "";
    const peer_host_name = draft.topology === "2vm" ? draft.peer_host_name.trim() : "";
    const observability_vm_ip = draft.topology === "2vm" ? draft.observability_vm_ip.trim() : "";
    await save({
      topology: draft.topology,
      peer_host_ip,
      peer_host_name,
      observability_vm_ip,
      current_step: "credentials",
    });
    navigate("/wizard/credentials");
  }

  return (
    <>
      <Title>How many servers?</Title>
      <Lede>
        Pick the layout for this customer. Most KIN Mail deployments use two servers for high
        availability; one server is fine when the platform already keeps the host redundant.
      </Lede>
      <ChoiceGrid>
        <Choice
          type="button"
          selected={draft.topology === "1vm"}
          onClick={() => void pick("1vm")}
        >
          <strong>1 server</strong>
          <span>
            Single-node install. Best when the virtualization platform already provides host-level
            redundancy.
          </span>
        </Choice>
        <Choice
          type="button"
          selected={draft.topology === "2vm"}
          onClick={() => void pick("2vm")}
        >
          <strong>2 servers (recommended)</strong>
          <span>
            Active-passive high availability with live data replication and automatic failover.
          </span>
        </Choice>
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
              placeholder="10.10.40.14"
              autoComplete="off"
            />
            <Hint>
              IP of the peer host for this HA pair. Connectivity is not checked here — that comes
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
              placeholder="10.10.40.12"
              autoComplete="off"
            />
            <Hint>
              Independent witness for quorum (qdevice) plus monitoring. Same step as the second
              mail server because a 2-server HA pair is not complete without it.
            </Hint>
          </FieldRow>
        </>
      ) : null}
      <Hint>Larger topologies are not offered in this wizard yet.</Hint>
      <Err>{fieldErr || error}</Err>
      <NavRow>
        <span />
        <Button type="button" disabled={saving} onClick={() => void next()}>
          Continue
        </Button>
      </NavRow>
    </>
  );
}
