import type { ReactNode } from "react";
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
import { WizardProvider } from "./wizard/WizardContext";
import WizardLayout from "./wizard/WizardLayout";
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
import { Lede, Shell, Spinner } from "./ui";

function LoadingShell({ text }: { text: string }) {
  return (
    <Shell>
      <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "0.85rem" }}>
        <Spinner $size={20} />
        <Lede style={{ margin: 0 }}>{text}</Lede>
      </div>
    </Shell>
  );
}

function RequireEula({ children }: { children: ReactNode }) {
  const { accepted, loading } = useEula();
  const { deployed, loading: setupLoading } = useSetup();
  const location = useLocation();
  if (loading || setupLoading) return <LoadingShell text="Loading…" />;
  if (deployed) return <>{children}</>;
  if (!accepted) return <Navigate to="/eula" replace state={{ from: location }} />;
  return <>{children}</>;
}

function RequireAuth({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth();
  if (loading) return <LoadingShell text="Checking session…" />;
  if (!user) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

/** Wizard only: skip login before setup is complete (incl. mid full-install). */
function RequireAuthIfDeployed({ children }: { children: ReactNode }) {
  const { deployed, loading: setupLoading } = useSetup();
  const { user, loading: authLoading } = useAuth();
  if (setupLoading || authLoading) return <LoadingShell text="Checking setup…" />;
  if (deployed && !user) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

/** Bare /wizard → Deploy while install runs, Cluster once deployed, else topology. */
function WizardIndexRedirect() {
  const { deployed, installInProgress, loading } = useSetup();
  if (loading) return <LoadingShell text="Checking setup…" />;
  const dest = wizardHomePath(installInProgress, deployed);
  if (!dest.startsWith("/wizard/")) {
    return <Navigate to={dest} replace />;
  }
  const rel = dest.replace(/^\/wizard\/?/, "") || "topology";
  return <Navigate to={rel} replace />;
}

function HomeRedirect() {
  const { deployed, installInProgress, loading } = useSetup();
  if (loading) return <LoadingShell text="Checking setup…" />;
  return <Navigate to={wizardHomePath(installInProgress, deployed)} replace />;
}

export default function App() {
  return (
    <SetupProvider>
      <EulaProvider>
        <AuthProvider>
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
        </AuthProvider>
      </EulaProvider>
    </SetupProvider>
  );
}
