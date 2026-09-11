import { useEffect, useRef, useState, type ReactNode } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { AuthProvider, useAuth } from "./auth";
import { EulaProvider, useEula } from "./eula";
import { SetupProvider, useSetup, wizardHomePath } from "./setup";
import ClusterPage from "./pages/Cluster";
import CreateMailboxPage from "./pages/CreateMailbox";
import EulaPage from "./pages/Eula";
import LoginPage from "./pages/Login";
import SettingsPage from "./pages/Settings";
import UsersPage from "./pages/Users";
import SecurityPage from "./pages/Security";
import MailGatewayPage from "./pages/MailGateway";
import { WizardProvider } from "./wizard/WizardContext";
import WizardLayout from "./wizard/WizardLayout";
import AddSecondServerPage from "./wizard/AddSecondServer";
import CredentialsStep from "./wizard/steps/CredentialsStep";
import DeployStep from "./wizard/steps/DeployStep";
import DeployLogsPage from "./wizard/steps/DeployLogsPage";
import DomainStep from "./wizard/steps/DomainStep";
import FirewallStep from "./wizard/steps/FirewallStep";
import HybridStep from "./wizard/steps/HybridStep";
import LicensingStep from "./wizard/steps/LicensingStep";
import ReviewStep from "./wizard/steps/ReviewStep";
import TlsStep from "./wizard/steps/TlsStep";
import TopologyStep from "./wizard/steps/TopologyStep";
import ZpushStep from "./wizard/steps/ZpushStep";
import { DeploySessionProvider } from "./wizard/DeploySession";
import { isHaOrchestrationLog } from "./wizard/deployPipeline";
import { BrandLockup, Lede, Shell, Spinner } from "./ui";
import { TaskProvider } from "./tasks/TaskProvider";
import { AlertProvider } from "./tasks/AlertProvider";
import ActivityCenterPage from "./pages/ActivityCenter";

function LoadingShell({ text }: { text: string }) {
  return (
    <Shell>
      {/* The lockup keeps a slow /api/setup/status from looking like a blank
          page. It is the same mark the rail and the gate screens use. */}
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          gap: "1.25rem",
        }}
      >
        <BrandLockup compact />
        <div style={{ display: "flex", alignItems: "center", gap: "0.6rem" }}>
          <Spinner $size={16} />
          <Lede style={{ margin: 0 }}>{text}</Lede>
        </div>
      </div>
    </Shell>
  );
}

function RequireEula({ children }: { children: ReactNode }) {
  const { accepted, loading } = useEula();
  const { deployed, loading: setupLoading } = useSetup();
  const location = useLocation();
  if (loading || setupLoading) return <LoadingShell text="Loading..." />;
  if (deployed) return <>{children}</>;
  if (!accepted) return <Navigate to="/eula" replace state={{ from: location }} />;
  return <>{children}</>;
}

function RequireAuth({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth();
  if (loading) return <LoadingShell text="Checking session..." />;
  if (!user) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

/**
 * Wizard only: skip login before setup is complete (incl. mid full-install).
 *
 * The moment `deployed` flips true, the backend stops accepting the
 * anonymous pre-deploy identity - but the operator is usually still looking
 * at the just-finished Deploy step. Wait a grace period after that flip
 * before sending them to /login, so a real HCI/on-site install doesn't feel
 * like it yanks the screen away the instant the pipeline finishes.
 */
const POST_DEPLOY_LOGIN_GRACE_MS = 30_000;

function RequireAuthIfDeployed({ children }: { children: ReactNode }) {
  const { deployed, wizardRequiresLogin, loading: setupLoading } = useSetup();
  const { user, loading: authLoading } = useAuth();
  const [graceElapsed, setGraceElapsed] = useState(false);
  const deployedAtRef = useRef<number | null>(null);

  useEffect(() => {
    if (!deployed) {
      deployedAtRef.current = null;
      setGraceElapsed(false);
      return;
    }
    if (deployedAtRef.current === null) {
      deployedAtRef.current = Date.now();
    }
    const remaining = POST_DEPLOY_LOGIN_GRACE_MS - (Date.now() - deployedAtRef.current);
    if (remaining <= 0) {
      setGraceElapsed(true);
      return;
    }
    const timer = window.setTimeout(() => setGraceElapsed(true), remaining);
    return () => window.clearTimeout(timer);
  }, [deployed]);

  if (setupLoading || authLoading) return <LoadingShell text="Checking setup..." />;
  // Peer / password-file-gone: force login immediately (no post-deploy grace).
  if (wizardRequiresLogin && !deployed && !user) {
    return <Navigate to="/login" replace />;
  }
  if (deployed && !user && graceElapsed) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

/** Bare /wizard → Deploy while install runs, Cluster once deployed, else topology. */
function WizardIndexRedirect() {
  const { deployed, installInProgress, loading } = useSetup();
  if (loading) return <LoadingShell text="Checking setup..." />;
  const dest = wizardHomePath(installInProgress, deployed);
  if (!dest.startsWith("/wizard/")) {
    return <Navigate to={dest} replace />;
  }
  const rel = dest.replace(/^\/wizard\/?/, "") || "topology";
  return <Navigate to={rel} replace />;
}

function HomeRedirect() {
  const { deployed, installInProgress, loading } = useSetup();
  if (loading) return <LoadingShell text="Checking setup..." />;
  return <Navigate to={wizardHomePath(installInProgress, deployed)} replace />;
}

export default function App() {
  return (
    <SetupProvider>
      <EulaProvider>
        <AuthProvider>
          <TaskProvider>
          <AlertProvider>
          <Routes>
            <Route path="/eula" element={<EulaPage />} />
            <Route
              path="/login"
              element={
                <RequireEula>
                  <LoginPage />
                </RequireEula>
              }
            />
            <Route
              path="/cluster"
              element={
                <RequireEula>
                  <RequireAuth>
                    <ClusterPage />
                  </RequireAuth>
                </RequireEula>
              }
            />
            <Route
              path="/cluster/add-second-server"
              element={
                <RequireEula>
                  <RequireAuth>
                    <WizardProvider>
                      <DeploySessionProvider logFilter={isHaOrchestrationLog}>
                        <AddSecondServerPage />
                      </DeploySessionProvider>
                    </WizardProvider>
                  </RequireAuth>
                </RequireEula>
              }
            />
            <Route
              path="/mailboxes"
              element={
                <RequireEula>
                  <RequireAuth>
                    <CreateMailboxPage />
                  </RequireAuth>
                </RequireEula>
              }
            />
            <Route
              path="/users"
              element={
                <RequireEula>
                  <RequireAuth>
                    <UsersPage />
                  </RequireAuth>
                </RequireEula>
              }
            />
            <Route
              path="/security"
              element={
                <RequireEula>
                  <RequireAuth>
                    <SecurityPage />
                  </RequireAuth>
                </RequireEula>
              }
            />
            <Route
              path="/activity"
              element={
                <RequireEula>
                  <RequireAuth>
                    <ActivityCenterPage />
                  </RequireAuth>
                </RequireEula>
              }
            />
            <Route
              path="/mail-gateway"
              element={
                <RequireEula>
                  <RequireAuth>
                    <MailGatewayPage />
                  </RequireAuth>
                </RequireEula>
              }
            />
            <Route
              path="/settings"
              element={
                <RequireEula>
                  <RequireAuth>
                    <SettingsPage />
                  </RequireAuth>
                </RequireEula>
              }
            />
            <Route
              path="/wizard"
              element={
                <RequireEula>
                  <RequireAuthIfDeployed>
                    <WizardProvider>
                      <DeploySessionProvider>
                        <WizardLayout />
                      </DeploySessionProvider>
                    </WizardProvider>
                  </RequireAuthIfDeployed>
                </RequireEula>
              }
            >
              <Route index element={<WizardIndexRedirect />} />
              <Route path="topology" element={<TopologyStep />} />
              <Route path="credentials" element={<CredentialsStep />} />
              <Route path="domain" element={<DomainStep />} />
              <Route path="tls" element={<TlsStep />} />
              <Route path="hybrid" element={<HybridStep />} />
              <Route path="zpush" element={<ZpushStep />} />
              <Route path="licensing" element={<LicensingStep />} />
              <Route path="firewall" element={<FirewallStep />} />
              <Route path="review" element={<ReviewStep />} />
              <Route path="deploy" element={<DeployStep />} />
              <Route path="deploy/logs" element={<DeployLogsPage />} />
            </Route>
            <Route path="/" element={<HomeRedirect />} />
            <Route path="*" element={<HomeRedirect />} />
          </Routes>
          </AlertProvider>
          </TaskProvider>
        </AuthProvider>
      </EulaProvider>
    </SetupProvider>
  );
}
