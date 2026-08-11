import { useNavigate } from "react-router-dom";
import { Button, Choice, ChoiceGrid, Err, Hint, Lede, NavRow, Title } from "../../ui";
import { useWizard } from "../WizardContext";
import type { WizardDraft } from "../types";

export default function TopologyStep() {
  const { draft, setLocal, save, error, saving } = useWizard();
  const navigate = useNavigate();

  async function pick(topology: WizardDraft["topology"]) {
    setLocal({ topology, current_step: "topology" });
  }

  async function next() {
    if (!draft.topology) return;
    await save({ topology: draft.topology, current_step: "domain" });
    navigate("/wizard/domain");
  }

  return (
    <>
      <Title>Topology</Title>
      <Lede>
        Choose how many VMs this deployment will use. This matches the operator model in the
        topology design brief — 1-VM for infrastructure HA, 2-VM for KIN Mail&apos;s own
        DRBD/Pacemaker HA.
      </Lede>
      <ChoiceGrid>
        <Choice
          type="button"
          selected={draft.topology === "1vm"}
          onClick={() => void pick("1vm")}
        >
          <strong>1 VM</strong>
          <span>
            Single-node install. Use when the underlying platform already provides host-level
            redundancy (true HCI). No DRBD/Pacemaker stack.
          </span>
        </Choice>
        <Choice
          type="button"
          selected={draft.topology === "2vm"}
          onClick={() => void pick("2vm")}
        >
          <strong>2 VM</strong>
          <span>
            Active-passive HA with DRBD replication, Pacemaker, and SBD fencing. Primary target
            mode for most customers.
          </span>
        </Choice>
      </ChoiceGrid>
      <Hint>3-VM topology is deferred and not offered in this wizard.</Hint>
      <Err>{error}</Err>
      <NavRow>
        <span />
        <Button type="button" disabled={!draft.topology || saving} onClick={() => void next()}>
          Continue
        </Button>
      </NavRow>
    </>
  );
}
