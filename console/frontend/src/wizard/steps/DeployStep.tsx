import { useEffect, useState } from "react";
import { DnsRecordsPanel, DkimPublishBox } from "../DnsRecordsPanel";
import { parseDkimFromInstallLog, type DkimDnsRecord } from "../mailDns";
import { useNavigate } from "react-router-dom";
import styled from "@emotion/styled";
import { isOpsRole, useAuth } from "../../auth";
import { useSetup } from "../../setup";
import { api } from "../../api";
import {
  Button,
  Hint,
  Lede,
  NavRow,
  Skeleton,
  Spinner,
  Title,
  WarnBox,
} from "../../ui";
import { theme } from "../../styles/theme";
import { useWizard } from "../WizardContext";
import { useDeploySession } from "../DeploySession";
import {
  FULL_INSTALL_STAGES,
  HA_ORCH_STAGES,
  isHaOrchestrationLog,
  shouldOfferHaPair,
} from "../deployPipeline";

const StepCard = styled.div`
  border: 1px solid ${theme.line};
  background: ${theme.bgElev};
  border-radius: ${theme.radius.md};
  padding: 1rem 1.05rem;
  margin-bottom: 0.85rem;
  transition:
    box-shadow ${theme.motion.fast} ease-out,
    border-color ${theme.motion.fast} ease-out;

  &:hover {
    box-shadow: 0 8px 24px rgba(15, 23, 42, 0.06);
  }
`;

const StepHeading = styled.p`
  margin: 0 0 0.35rem;
  font-weight: 650;
  font-size: 0.95rem;
`;

const StepBody = styled.p`
  margin: 0 0 0.85rem;
  color: ${theme.muted};
  font-size: 0.86rem;
  line-height: 1.45;
`;

const ProgressCard = styled.div`
  border: 1px solid ${theme.line};
  background: ${theme.bgElev};
  border-radius: ${theme.radius.md};
  padding: 1.1rem 1.15rem;
  margin-bottom: 1rem;
`;

const ProgressHead = styled.div`
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 1rem;
  margin-bottom: 0.65rem;
`;

const ProgressLabel = styled.p`
  margin: 0;
  font-size: 0.92rem;
  font-weight: 650;
  color: ${theme.ink};
`;

const ProgressCounter = styled.span`
  font-size: 0.82rem;
  color: ${theme.muted};
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
`;

const Track = styled.div`
  height: 8px;
  border-radius: 999px;
  background: ${theme.bgPanel};
  border: 1px solid ${theme.line};
  overflow: hidden;
`;

const Fill = styled.div<{ $pct: number; $failed?: boolean; $complete?: boolean }>`
  height: 100%;
  width: ${(p) => Math.min(100, Math.max(0, p.$pct))}%;
  border-radius: 999px;
  background: ${(p) => (p.$failed ? theme.danger : p.$complete ? theme.ok : theme.accent)};
  transition:
    width ${theme.motion.slow} ease-out,
    background ${theme.motion.fast} ease-out;
`;

const StageMeta = styled.p`
  margin: 0.55rem 0 0;
  font-size: 0.82rem;
  color: ${theme.muted};
  line-height: 1.4;
`;

const StageList = styled.ol`
  margin: 0.85rem 0 0;
  padding: 0;
  list-style: none;
  display: grid;
  gap: 0.35rem;
`;

const StageItem = styled.li<{ $state: "pending" | "active" | "done" | "failed" }>`
  display: flex;
  align-items: center;
  gap: 0.55rem;
  font-size: 0.8rem;
  color: ${(p) =>
    p.$state === "active"
      ? theme.ink
      : p.$state === "done"
        ? theme.ok
        : p.$state === "failed"
          ? theme.danger
          : theme.muted};
  font-weight: ${(p) => (p.$state === "active" ? 600 : 500)};
`;

const Dot = styled.span<{ $state: "pending" | "active" | "done" | "failed" }>`
  width: 0.55rem;
  height: 0.55rem;
  border-radius: 999px;
  flex-shrink: 0;
  background: ${(p) =>
    p.$state === "active"
      ? theme.accent
      : p.$state === "done"
        ? theme.ok
        : p.$state === "failed"
          ? theme.danger
          : theme.line};
  box-shadow: ${(p) => (p.$state === "active" ? `0 0 0 3px ${theme.accentSoft}` : "none")};
  transition:
    background ${theme.motion.fast} ease-out,
    box-shadow ${theme.motion.fast} ease-out;
`;

const ActionsRow = styled.div`
  display: flex;
  flex-wrap: wrap;
  gap: 0.65rem;
  align-items: center;
  margin-top: 0.85rem;
`;

const DnsReference = styled.details`
  margin: 0 0 1rem;
  border: 1px solid ${theme.line};
  background: ${theme.bgElev};
  border-radius: ${theme.radius.md};
  padding: 0.65rem 0.9rem;
`;

const DnsReferenceSummary = styled.summary`
  cursor: pointer;
  font-size: 0.86rem;
  font-weight: 600;
  color: ${theme.muted};
`;

type HaDisk = {
  ok: boolean;
  build_allowed?: boolean;
  errors: string[];
  instructions?: string;
  will_auto_partition?: { label?: string; disk?: string; message?: string; size_human?: string }[];
};

function HaDiskStatus({ haDisk, haDiskLoading }: { haDisk: HaDisk | null; haDiskLoading: boolean }) {
  return (
    <>
      {haDiskLoading && (
        <div style={{ marginBottom: "0.75rem" }}>
          <Skeleton $h="0.7rem" $w="78%" style={{ marginBottom: 8 }} />
          <Skeleton $h="0.7rem" $w="54%" />
          <Hint style={{ margin: "0.55rem 0 0" }}>
            Checking second-disk partitions on both mail servers…
          </Hint>
        </div>
      )}
      {haDisk && !haDisk.ok && haDisk.build_allowed !== true && (
        <WarnBox>
          <strong>Second disk not ready for DRBD.</strong> Build HA pair stays blocked until
          both mail VMs have a unique blank spare disk (or the proven GPT layout).
          <ul style={{ margin: "0.5rem 0 0", paddingLeft: "1.2rem" }}>
            {(haDisk.errors || []).map((e) => (
              <li key={e}>{e}</li>
            ))}
          </ul>
          {haDisk.instructions ? (
            <p style={{ margin: "0.65rem 0 0" }}>{haDisk.instructions}</p>
          ) : null}
        </WarnBox>
      )}
      {haDisk?.will_auto_partition && haDisk.will_auto_partition.length > 0 ? (
        <WarnBox>
          <strong>Build HA pair will partition a blank spare disk.</strong> The selector
          found exactly one unused disk that is not the OS disk, has no partition table,
          and is ≥20 GiB. GPT: partition 1 = Zimbra/DRBD data, partition 2 ≈ 256 MiB meta
          (no mkfs). --check / dry-run will not write.
          <ul style={{ margin: "0.5rem 0 0", paddingLeft: "1.2rem" }}>
            {haDisk.will_auto_partition.map((p) => (
              <li key={`${p.label || ""}-${p.disk || p.message || ""}`}>
                {p.label ? <strong>{p.label}: </strong> : null}
                {p.message || `${p.disk} (${p.size_human || ""})`}
              </li>
            ))}
          </ul>
          {(haDisk.errors || []).length > 0 ? (
            <p style={{ margin: "0.65rem 0 0" }}>
              After partitioning, Build HA re-checks. Remaining issues (for example Zimbra
              still on the OS volume) still stop DRBD:
            </p>
          ) : null}
          {(haDisk.errors || []).length > 0 ? (
            <ul style={{ margin: "0.5rem 0 0", paddingLeft: "1.2rem" }}>
              {haDisk.errors.map((e) => (
                <li key={e}>{e}</li>
              ))}
            </ul>
          ) : null}
        </WarnBox>
      ) : null}
      {haDisk?.ok && (
        <Hint style={{ marginTop: 0 }}>
          DRBD disks look ready ({"/dev/sdb1"} data, {"/dev/sdb2"} meta) on the servers we
          could inspect.
        </Hint>
      )}
    </>
  );
}

function stageState(
  index: number,
  current: number,
  failed: boolean,
  complete: boolean,
): "pending" | "active" | "done" | "failed" {
  const n = index + 1;
  if (complete) return "done";
  if (failed && n === current) return "failed";
  if (current > 0 && n < current) return "done";
  if (n === current) return "active";
  return "pending";
}

export default function DeployStep() {
  const { draft } = useWizard();
  const { user } = useAuth();
  const { deployed, installInProgress, fullInstallComplete, haSetupComplete, loading } = useSetup();
  const navigate = useNavigate();
  const canOps = !deployed || isOpsRole(user?.role);
  const {
    message,
    pipelineBusy,
    log,
    cancelBusy,
    deadmanHint,
    installProgress,
    confirmFull,
    setConfirmFull,
    confirmHa,
    setConfirmHa,
    runDeploy,
    runHaOrchestration,
    runCancelDeadman,
    openLogsTab,
  } = useDeploySession();

  const { current, total, label, complete, failed } = installProgress;
  const haLog = isHaOrchestrationLog(log);
  const [maintNodes, setMaintNodes] = useState<string[]>([]);
  const [dkim, setDkim] = useState<DkimDnsRecord | null>(null);
  useEffect(() => {
    const parsed = parseDkimFromInstallLog(log);
    if (parsed) setDkim(parsed);
  }, [log]);
  useEffect(() => {
    if (!deployed) return;
    void api<{ cluster?: { standby?: string[] } }>("/api/cluster/status")
      .then((st) => setMaintNodes(st.cluster?.standby || []))
      .catch(() => setMaintNodes([]));
  }, [deployed]);
  const inMaintenance = maintNodes.length > 0;
  const activeRun = pipelineBusy || installInProgress;
  const pct = complete ? 100 : current > 0 ? (current / total) * 100 : activeRun ? 4 : 0;
  const showProgress = activeRun || current > 0 || complete || failed;
  // Hide setup cards while any install is active on this host (SSE or server-side).
  // After setup-complete, never offer Deploy again (would re-run kin-mail.sh on live mail).
  const showSetupCards = canOps && !activeRun && !fullInstallComplete;

  const [haDisk, setHaDisk] = useState<HaDisk | null>(null);
  const [haDiskLoading, setHaDiskLoading] = useState(false);
  useEffect(() => {
    if (draft.topology !== "2vm" || !canOps) return;
    // Fetch after a successful Deploy even if the 5s install-in-progress poll has not
    // cleared yet; the continue-to-HA prompt needs disk status on the same page.
    if (activeRun && !(complete && !failed)) return;
    let cancelled = false;
    setHaDiskLoading(true);
    api<HaDisk>("/api/wizard/ha-disk-preflight")
      .then((r) => {
        if (!cancelled) setHaDisk(r);
      })
      .catch((err) => {
        if (!cancelled) {
          setHaDisk({
            ok: false,
            errors: [err instanceof Error ? err.message : "Could not check disks"],
            instructions: "",
          });
        }
      })
      .finally(() => {
        if (!cancelled) setHaDiskLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [draft.topology, canOps, activeRun, complete, failed]);
  const haDiskBlocked =
    draft.topology === "2vm" && haDisk != null && !haDisk.ok && haDisk.build_allowed !== true;
  const showHaPair = canOps && !activeRun && shouldOfferHaPair({
    topology: draft.topology,
    fullInstallComplete,
  });
  const haPairBlocked =
    !confirmHa || inMaintenance || pipelineBusy || haDiskBlocked || haDiskLoading || haDisk == null;
  const haSummary = `I confirm HA orchestration should run for ${draft.peer_host_ip || "the second server"} (observability ${draft.observability_vm_ip || "unset"}, VIP ${draft.cluster_vip_ip || "unset"}).`;
  const recheckHaDisk = () => {
    setHaDiskLoading(true);
    void api<HaDisk>("/api/wizard/ha-disk-preflight")
      .then(setHaDisk)
      .catch((err) =>
        setHaDisk({
          ok: false,
          errors: [err instanceof Error ? err.message : "Could not check disks"],
        }),
      )
      .finally(() => setHaDiskLoading(false));
  };

  return (
    <>
      <Title>Perform deployment</Title>
      <Lede>
        Guided setup for this server. Settings are saved first, then the mail system is installed.
        {!canOps && (
          <>
            {" "}
            Your role (<strong>{user?.role_label || "Customer Admin"}</strong>) cannot run deploy;
            ask a KIN Super Admin or Support-Ops.
          </>
        )}
      </Lede>

      {loading ? null : fullInstallComplete ? (
        <DnsReference>
          <DnsReferenceSummary>DNS records (already configured, click to view again)</DnsReferenceSummary>
          <div style={{ marginTop: "0.75rem" }}>
            <DnsRecordsPanel
              mailDomain={draft.mail_domain}
              mailHost={draft.mail_host}
              ruaEmail={draft.le_email}
              preferredAIp={draft.topology === "2vm" ? draft.cluster_vip_ip : ""}
              installLog={log}
              dkimPendingNote={!dkim}
            />
            <DkimPublishBox dkim={dkim} mailDomain={draft.mail_domain} />
          </div>
        </DnsReference>
      ) : (
        <>
          <DnsRecordsPanel
            mailDomain={draft.mail_domain}
            mailHost={draft.mail_host}
            ruaEmail={draft.le_email}
            preferredAIp={draft.topology === "2vm" ? draft.cluster_vip_ip : ""}
            installLog={log}
            dkimPendingNote={!dkim}
          />
          <DkimPublishBox dkim={dkim} mailDomain={draft.mail_domain} />
        </>
      )}

      {inMaintenance && (
        <WarnBox>
          A mail node is in maintenance ({maintNodes.join(", ")}). Deploy is blocked until you Exit
          Maintenance on the Cluster page.
        </WarnBox>
      )}

      {showProgress && (
        <ProgressCard>
          <ProgressHead>
            <ProgressLabel>
              {failed
                ? haLog
                  ? "HA sequence failed"
                  : "Deployment failed"
                : complete
                  ? haLog
                    ? "HA sequence complete"
                    : "Deployment complete"
                  : activeRun
                    ? haLog
                      ? "HA sequence in progress"
                      : "Deployment in progress"
                    : "Deployment status"}
            </ProgressLabel>
            <ProgressCounter>
              Step {current}/{total}
            </ProgressCounter>
          </ProgressHead>
          <Track>
            <Fill $pct={pct} $failed={failed} $complete={complete} />
          </Track>
          <StageMeta>
            {failed
              ? `Stopped at: ${label}`
              : complete
                ? haLog
                  ? "HA playbooks finished."
                  : "All install stages finished."
                : current > 0
                  ? `Current: ${label}`
                  : "Preparing install…"}
          </StageMeta>
          <StageList aria-label={haLog ? "HA orchestration steps" : "Install stages"}>
            {(haLog ? HA_ORCH_STAGES : FULL_INSTALL_STAGES).map((stage, idx) => {
              const state = stageState(idx, current, failed, complete);
              return (
                <StageItem key={stage.script} $state={state}>
                  <Dot $state={state} />
                  <span>
                    {idx + 1}. {stage.label}
                  </span>
                </StageItem>
              );
            })}
          </StageList>
          <ActionsRow>
            <Button type="button" variant="ghost" onClick={openLogsTab}>
              View logs
            </Button>
          </ActionsRow>
        </ProgressCard>
      )}

      {fullInstallComplete && canOps && !activeRun && (
        <StepCard>
          <StepHeading>Mail is already installed on this host</StepHeading>
          <StepBody>
            Deploy stays hidden so a second full-install cannot start on live mail. On a 2-server
            pair continue with Build HA pair below. After a failed HA attempt, fix then re-run
            Build HA pair from the top; do not click Deploy.
          </StepBody>
        </StepCard>
      )}

      {showSetupCards && (
        <>
          <StepCard>
            <StepHeading>1. Deploy the mail system</StepHeading>
            <StepBody>
              Saves settings again if needed, then installs and configures mail on this host. This
              can take a long time; use View logs for the live stream (opens in a new tab).
            </StepBody>
            <label
              style={{
                display: "flex",
                gap: "0.55rem",
                alignItems: "flex-start",
                margin: "0 0 0.75rem",
                fontSize: "0.88rem",
                lineHeight: 1.4,
                cursor: "pointer",
              }}
            >
              <input
                type="checkbox"
                checked={confirmFull}
                onChange={(e) => setConfirmFull(e.target.checked)}
                style={{ marginTop: "0.2rem" }}
              />
              <span>I confirm Deploy should run on this server now.</span>
            </label>
            <ActionsRow style={{ marginTop: 0 }}>
              <Button
                type="button"
                disabled={!confirmFull || inMaintenance}
                onClick={() => void runDeploy()}
              >
                Deploy
              </Button>
              {!showProgress && (
                <Button type="button" variant="ghost" onClick={openLogsTab}>
                  View logs
                </Button>
              )}
            </ActionsRow>
          </StepCard>
        </>
      )}

      {showHaPair && (
        <StepCard>
          <StepHeading>Build the 2-server HA pair</StepHeading>
          <StepBody>
            After this host has mail installed, run the Ansible sequence against the second
            server (OS hardening, qnetd, Corosync cluster setup, qdevice, fencing, DRBD,
            Pacemaker). Progress streams into View logs with a checkpoint per playbook. A
            failure stops there; nothing is retried or rolled back automatically. After a
            manual fix, run this again from the top; the playbooks are idempotent.
            {haSetupComplete
              ? " HA is already marked complete on this host. Re-run only after a failed attempt that you have already fixed."
              : " Do not click Deploy on this host; mail is already installed."}
          </StepBody>
          <HaDiskStatus haDisk={haDisk} haDiskLoading={haDiskLoading} />
          <label
            style={{
              display: "flex",
              gap: "0.55rem",
              alignItems: "flex-start",
              margin: "0 0 0.75rem",
              fontSize: "0.88rem",
              lineHeight: 1.4,
              cursor: "pointer",
            }}
          >
            <input
              type="checkbox"
              checked={confirmHa}
              onChange={(e) => setConfirmHa(e.target.checked)}
              style={{ marginTop: "0.2rem" }}
            />
            <span>{haSummary}</span>
          </label>
          <ActionsRow>
            <Button
              type="button"
              disabled={haPairBlocked}
              onClick={() => void runHaOrchestration()}
            >
              Build HA pair
            </Button>
            <Button
              type="button"
              variant="ghost"
              disabled={haDiskLoading || pipelineBusy}
              onClick={recheckHaDisk}
            >
              Recheck disks
            </Button>
          </ActionsRow>
          {pipelineBusy ? (
            <Hint style={{ marginTop: "0.65rem" }}>A job is already running. Wait for it to finish.</Hint>
          ) : haDiskLoading || haDisk == null ? (
            <Hint style={{ marginTop: "0.65rem" }}>Disk check is still running. Build HA pair stays disabled until it finishes.</Hint>
          ) : !confirmHa ? (
            <Hint style={{ marginTop: "0.65rem" }}>Tick the confirmation box to enable Build HA pair.</Hint>
          ) : null}
        </StepCard>
      )}

      {canOps && (deadmanHint || cancelBusy) && (
        <StepCard>
          <StepHeading>3. Confirm admin access still works</StepHeading>
          <StepBody>
            After the firewall step runs, verify you can still reach this console and SSH, then
            confirm below so the temporary safety timer is cancelled.
          </StepBody>
          <WarnBox style={{ marginBottom: "0.75rem" }}>
            <strong>Safety timer may be active.</strong> Confirm only after you have checked
            access.
          </WarnBox>
          <Button
            type="button"
            variant="danger"
            loading={cancelBusy}
            onClick={() => void runCancelDeadman()}
          >
            {cancelBusy ? "Confirming…" : "Confirm access is OK"}
          </Button>
        </StepCard>
      )}

      {activeRun && !(complete && !failed) && (
        <Hint>
          <span style={{ display: "inline-flex", alignItems: "center", gap: "0.45rem" }}>
            <Spinner /> Working… open View logs in a new tab for the live stream. Stay on this page
            until the stage finishes; leaving does not stop the server-side job.
          </span>
        </Hint>
      )}

      {message && <Hint>{message}</Hint>}

      {!activeRun && (
        <NavRow>
          <Button type="button" variant="ghost" onClick={() => navigate("/wizard/review")}>
            Back
          </Button>
          <span />
        </NavRow>
      )}
    </>
  );
}
