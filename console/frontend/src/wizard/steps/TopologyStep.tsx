import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button, Choice, ChoiceGrid, Err, Hint, Lede, NavRow, Title } from "../../ui";
import { useWizard } from "../WizardContext";
import type { WizardDraft } from "../types";

export default function TopologyStep() {
  const { draft, setLocal, save, error, saving } = useWizard();
  const navigate = useNavigate();
  const [fieldErr, setFieldErr] = useState("");

  async function pick(topology: WizardDraft["topology"]) {
    setFieldErr("");
    setLocal({ topology, current_step: "topology" });
  }

  async function next() {
    if (!draft.topology) {
      setFieldErr("Choose 1 VM or 2 VM before continuing.");
      return;
    }
    setFieldErr("");
    await save({ topology: draft.topology, current_step: "domain" });
    navigate("/wizard/domain");
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
