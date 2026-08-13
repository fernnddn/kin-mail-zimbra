import styled from "@emotion/styled";
import { Link, Navigate, Outlet, useLocation } from "react-router-dom";
import { ConsoleChrome } from "../ConsoleChrome";
import { useSetup } from "../setup";
import { theme } from "../styles/theme";
import { useWizard } from "./WizardContext";
import { WIZARD_STEPS, stepIndex, type StepId } from "./types";

const Body = styled.div`
  flex: 1;
  display: grid;
  grid-template-columns: 260px 1fr;
  min-height: 0;

  @media (max-width: 860px) {
    grid-template-columns: 1fr;
  }
`;

const Sidebar = styled.aside`
  background: ${theme.sidebar};
  border-right: 1px solid ${theme.line};
  padding: 1.25rem 0.85rem;
  overflow: auto;

  @media (max-width: 860px) {
    border-right: 0;
    border-bottom: 1px solid ${theme.line};
  }
`;

const SideTitle = styled.p`
  margin: 0 0.55rem 0.75rem;
  font-size: 0.72rem;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: ${theme.muted};
`;

const StepLink = styled(Link)<{ $active?: boolean; $done?: boolean }>`
  display: flex;
  align-items: center;
  gap: 0.65rem;
  padding: 0.55rem 0.65rem;
  margin-bottom: 0.2rem;
  border-radius: ${theme.radius};
  text-decoration: none;
  color: ${(p) => (p.$active ? theme.ink : theme.muted)};
  background: ${(p) => (p.$active ? theme.accentSoft : "transparent")};
  border: 1px solid ${(p) => (p.$active ? "color-mix(in srgb, " + theme.accent + " 35%, transparent)" : "transparent")};
  font-size: 0.9rem;
  transition:
    color ${theme.motion} ease-out,
    background ${theme.motion} ease-out,
    border-color ${theme.motion} ease-out,
    box-shadow ${theme.motion} ease-out;

  &:hover {
    color: ${theme.ink};
    background: ${(p) => (p.$active ? theme.accentSoft : "rgba(0, 97, 255, 0.05)")};
    box-shadow: 0 4px 14px rgba(15, 23, 42, 0.05);
  }
`;

const StepNum = styled.span<{ $active?: boolean; $done?: boolean }>`
  width: 1.45rem;
  height: 1.45rem;
  border-radius: 999px;
  display: grid;
  place-items: center;
  font-size: 0.72rem;
  font-weight: 700;
  flex-shrink: 0;
  background: ${(p) =>
    p.$active ? theme.accent : p.$done ? "rgba(5, 150, 105, 0.18)" : theme.bgElev};
  color: ${(p) => (p.$active ? "#fff" : p.$done ? theme.ok : theme.muted)};
  border: 1px solid ${(p) => (p.$active ? theme.accent : theme.line)};
  transition:
    background ${theme.motion} ease-out,
    color ${theme.motion} ease-out,
    border-color ${theme.motion} ease-out;
`;

const Main = styled.main`
  min-width: 0;
  display: flex;
  flex-direction: column;
`;

const CrumbBar = styled.div`
  padding: 0.85rem 1.5rem 0;
  color: ${theme.muted};
  font-size: 0.82rem;

  a {
    color: ${theme.muted};
    text-decoration: none;
    transition: color ${theme.motion} ease-out;
  }

  a:hover {
    color: ${theme.ink};
  }

  strong {
    color: ${theme.ink};
    font-weight: 600;
  }
`;

const Content = styled.div`
  padding: 1rem 1.5rem 2rem;
  max-width: 760px;
  width: 100%;
  animation: kin-fade-in ${theme.motion} ease-out;
`;

function currentStepId(pathname: string): StepId {
  const parts = pathname.split("/").filter(Boolean);
  // /wizard/deploy and /wizard/deploy/logs both map to Deploy in the sidebar.
  if (parts.includes("deploy")) return "deploy";
  const part = parts[parts.length - 1] || "topology";
  const found = WIZARD_STEPS.find((s) => s.id === part);
  return found ? found.id : "topology";
}

export default function WizardLayout() {
  const { loading } = useWizard();
  const { deployed, installInProgress } = useSetup();
  const location = useLocation();
  const isDeployLogs = /\/wizard\/deploy\/logs\/?$/.test(location.pathname);
  const isDeploy = /\/wizard\/deploy\/?$/.test(location.pathname) || isDeployLogs;
  const active = currentStepId(location.pathname);
  const activeIdx = stepIndex(active);
  const activeDef = WIZARD_STEPS[activeIdx] || WIZARD_STEPS[0];

  // Mid-install always stays on Deploy. After deploy the operator can still
  // revisit topology (peer IP, Observability VM) and other steps.
  if (installInProgress && !isDeploy) {
    return <Navigate to="/wizard/deploy" replace />;
  }

  // Full-screen log viewer (often opened in a new browser tab).
  if (isDeployLogs) {
    return (
      <ConsoleChrome setupMode={!deployed}>
        <Outlet />
      </ConsoleChrome>
    );
  }

  return (
    <ConsoleChrome setupMode={!deployed}>
      <Body>
        <Sidebar>
          <SideTitle>Setup steps</SideTitle>
          {WIZARD_STEPS.map((step, idx) => {
            const done = idx < activeIdx;
            const isActive = step.id === active;
            return (
              <StepLink
                key={step.id}
                to={`/wizard/${step.id}`}
                $active={isActive}
                $done={done}
              >
                <StepNum $active={isActive} $done={done}>
                  {done ? "✓" : idx + 1}
                </StepNum>
                {step.label}
              </StepLink>
            );
          })}
        </Sidebar>
        <Main>
          <CrumbBar>
            <Link to="/wizard/topology">Setup</Link>
            {" / "}
            <strong>{activeDef.crumb}</strong>
          </CrumbBar>
          <Content key={active}>
            {loading ? <p style={{ color: theme.muted }}>Loading draft…</p> : <Outlet />}
          </Content>
        </Main>
      </Body>
    </ConsoleChrome>
  );
}
