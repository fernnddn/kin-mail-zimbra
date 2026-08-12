import { useNavigate } from "react-router-dom";
import styled from "@emotion/styled";
import { isOpsRole, useAuth } from "../../auth";
import { useSetup } from "../../setup";
import {
  Button,
  Hint,
  Lede,
  NavRow,
  OkMsg,
  Spinner,
  Title,
  WarnBox,
} from "../../ui";
import { theme } from "../../styles/theme";
import { useDeploySession } from "../DeploySession";
import { FULL_INSTALL_STAGES } from "../deployPipeline";

const StepCard = styled.div`
  border: 1px solid ${theme.line};
  background: ${theme.bgElev};
  border-radius: ${theme.radius};
  padding: 1rem 1.05rem;
  margin-bottom: 0.85rem;
  transition:
    box-shadow ${theme.motion} ease-out,
    border-color ${theme.motion} ease-out;

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
  border-radius: ${theme.radius};
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
    width ${theme.motion} ease-out,
    background ${theme.motion} ease-out;
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
    background ${theme.motion} ease-out,
    box-shadow ${theme.motion} ease-out;
`;

const ActionsRow = styled.div`
  display: flex;
  flex-wrap: wrap;
  gap: 0.65rem;
  align-items: center;
  margin-top: 0.85rem;
`;

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
  const { user } = useAuth();
  const { deployed, installInProgress } = useSetup();
  const navigate = useNavigate();
  const canOps = !deployed || isOpsRole(user?.role);
  const {
    message,
    okMessage,
    pipelineBusy,
    cancelBusy,
    deadmanHint,
    applyDone,
    installProgress,
    confirmFull,
    setConfirmFull,
    runApply,
    runDeploy,
    runCancelDeadman,
    openLogsTab,
  } = useDeploySession();

  const { current, total, label, complete, failed } = installProgress;
  const activeRun = pipelineBusy || installInProgress;
  const pct = complete ? 100 : current > 0 ? (current / total) * 100 : activeRun ? 4 : 0;
  const showProgress = activeRun || current > 0 || complete || failed;
  // Hide setup cards while any install is active on this host (SSE or server-side).
  const showSetupCards = canOps && !activeRun;

  return (
    <>
      <Title>Perform deployment</Title>
      <Lede>
        Guided setup for this server. Settings are saved first, then the mail system is installed.
        {!canOps && (
          <>
            {" "}
            Your role (<strong>{user?.role_label || "Customer Admin"}</strong>) cannot run deploy —
            ask a KIN Super Admin or Support-Ops.
          </>
        )}
      </Lede>

      {showProgress && (
        <ProgressCard>
          <ProgressHead>
            <ProgressLabel>
              {failed
                ? "Deployment failed"
                : complete
                  ? "Deployment complete"
                  : activeRun
                    ? "Deployment in progress"
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
                ? "All install stages finished."
                : current > 0
                  ? `Current: ${label}`
                  : "Preparing install…"}
          </StageMeta>
          <StageList aria-label="Install stages">
            {FULL_INSTALL_STAGES.map((stage, idx) => {
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

      {showSetupCards && (
        <>
          <StepCard>
            <StepHeading>1. Save your wizard settings</StepHeading>
            <StepBody>
              Writes the choices from this wizard to the server. Safe to run more than once — you
              will see whether anything actually changed.
            </StepBody>
            <Button type="button" variant="ghost" onClick={() => void runApply()}>
              Save settings
            </Button>
            {okMessage && (
              <OkMsg style={{ marginTop: "0.75rem", marginBottom: 0 }}>{okMessage}</OkMsg>
            )}
          </StepCard>

          <StepCard>
            <StepHeading>2. Deploy the mail system</StepHeading>
            <StepBody>
              Saves settings again if needed, then installs and configures mail on this host. This
              can take a long time — use View logs for the live stream (opens in a new tab).
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
                disabled={!confirmFull}
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
            {!applyDone && !showProgress && (
              <Hint style={{ marginTop: "0.75rem", marginBottom: 0 }}>
                Tip: you can Save settings alone first, or just press Deploy (it saves automatically).
              </Hint>
            )}
          </StepCard>
        </>
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
            disabled={cancelBusy}
            onClick={() => void runCancelDeadman()}
          >
            {cancelBusy ? (
              <>
                <Spinner /> Confirming…
              </>
            ) : (
              "Confirm access is OK"
            )}
          </Button>
        </StepCard>
      )}

      {activeRun && (
        <Hint>
          <span style={{ display: "inline-flex", alignItems: "center", gap: "0.45rem" }}>
            <Spinner /> Working… open View logs in a new tab for the live stream.
          </span>
        </Hint>
      )}

      {message && <Hint>{message}</Hint>}

      <NavRow>
        <Button type="button" variant="ghost" onClick={() => navigate("/wizard/review")}>
          Back
        </Button>
        <span />
      </NavRow>
    </>
  );
}
