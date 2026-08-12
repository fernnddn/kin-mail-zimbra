import type { ReactNode } from "react";
import { Navigate, Outlet, Route, Routes, useLocation } from "react-router-dom";
import { AuthProvider, useAuth } from "./auth";
import { EulaProvider, useEula } from "./eula";
import { SetupProvider, useSetup } from "./setup";
import AuditLogPage from "./pages/AuditLog";
import CreateMailboxPage from "./pages/CreateMailbox";
import EulaPage from "./pages/Eula";
import LoginPage from "./pages/Login";
import UsersPage from "./pages/Users";
import { WizardProvider } from "./wizard/WizardContext";
import WizardLayout from "./wizard/WizardLayout";
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
import { Lede, Shell } from "./ui";

function DeployOutlet() {
  return (
    <DeploySessionProvider>
      <Outlet />
    </DeploySessionProvider>
  );
}

function LoadingShell({ text }: { text: string }) {
  return (
    <Shell>
      <Lede>{text}</Lede>
    </Shell>
  );
}

function RequireEula({ children }: { children: ReactNode }) {
  const { accepted, loading } = useEula();
  const location = useLocation();
  if (loading) return <LoadingShell text="Loading…" />;
  if (!accepted) return <Navigate to="/eula" replace state={{ from: location }} />;
  return <>{children}</>;
}

function RequireAuth({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth();
  if (loading) return <LoadingShell text="Checking session…" />;
  if (!user) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

/** Wizard only: skip login before mail is deployed (/opt/zimbra absent). */
function RequireAuthIfDeployed({ children }: { children: ReactNode }) {
  const { deployed, loading: setupLoading } = useSetup();
  const { user, loading: authLoading } = useAuth();
  if (setupLoading || authLoading) return <LoadingShell text="Checking setup…" />;
  if (deployed && !user) return <Navigate to="/login" replace />;
  return <>{children}</>;
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
              path="/audit"
              element={
                <RequireEula>
                  <RequireAuth>
                    <AuditLogPage />
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
                      <WizardLayout />
                    </WizardProvider>
                  </RequireAuthIfDeployed>
                </RequireEula>
              }
            >
              <Route index element={<Navigate to="topology" replace />} />
              <Route path="topology" element={<TopologyStep />} />
              <Route path="domain" element={<DomainStep />} />
              <Route path="tls" element={<TlsStep />} />
              <Route path="hybrid" element={<HybridStep />} />
              <Route path="zpush" element={<ZpushStep />} />
              <Route path="licensing" element={<LicensingStep />} />
              <Route path="firewall" element={<FirewallStep />} />
              <Route path="review" element={<ReviewStep />} />
              <Route path="deploy" element={<DeployOutlet />}>
                <Route index element={<DeployStep />} />
                <Route path="logs" element={<DeployLogsPage />} />
              </Route>
            </Route>
            <Route path="/" element={<Navigate to="/wizard" replace />} />
            <Route path="*" element={<Navigate to="/wizard" replace />} />
          </Routes>
        </AuthProvider>
      </EulaProvider>
    </SetupProvider>
  );
}
