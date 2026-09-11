import { useEffect, useState, type ReactNode } from "react";
import styled from "@emotion/styled";
import { Link, NavLink, useNavigate } from "react-router-dom";
import { api } from "./api";
import { useAuth } from "./auth";
import { useSetup } from "./setup";
import { theme } from "./styles/theme";
import { Avatar, BrandLockup, Dropdown, focusRingOnDark, MenuItem } from "./ui";
import { TaskCenter } from "./tasks/TaskCenter";
import { useAlerts } from "./tasks/AlertProvider";

/* The ops shell: a charcoal rail down the left, paper to the right of it.
   The rail is where the operator lives, so it holds navigation, the two live
   surfaces (alerts and tasks) and the account menu, and it stays put while a
   long cluster page scrolls. */
const Frame = styled.div`
  min-height: 100vh;
  background: ${theme.paper};
  color: ${theme.ink};
  font-family: ${theme.font};
  display: grid;
  grid-template-columns: 232px minmax(0, 1fr);

  /* Below this the rail becomes a band across the top rather than a column.
     Nothing is hidden, it just stops being a sidebar. The rows must be sized
     explicitly: a single-column grid still stretches its rows over the 100vh
     min-height, which inflates the rail band to half the screen before the
     page itself starts. */
  @media (max-width: 860px) {
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: auto minmax(0, 1fr);
  }
`;

const SetupFrame = styled.div`
  min-height: 100vh;
  background: ${theme.paper};
  color: ${theme.ink};
  font-family: ${theme.font};
  display: flex;
  flex-direction: column;
`;

/* First boot: brand and nothing else. There is no cluster to navigate to and
   no account to manage until the install finishes. */
const SetupTop = styled.header`
  height: 3.25rem;
  display: flex;
  align-items: center;
  padding: 0 1.75rem;
  background: ${theme.bgElev};
  border-bottom: 1px solid ${theme.line};
  flex-shrink: 0;
`;

const Rail = styled.aside`
  background: ${theme.rail};
  color: ${theme.paper};
  display: flex;
  flex-direction: column;
  position: sticky;
  top: 0;
  height: 100vh;

  @media (max-width: 860px) {
    position: static;
    height: auto;
  }
`;

const RailBrand = styled.button`
  border: 0;
  background: transparent;
  cursor: pointer;
  font: inherit;
  text-align: left;
  padding: 0 1.25rem;
  height: 3.75rem;
  display: flex;
  align-items: center;
  flex-shrink: 0;
  border-bottom: 1px solid rgba(255, 255, 255, 0.1);

  ${focusRingOnDark}
`;

const RailNav = styled.nav`
  padding: 0.75rem 0;
  flex: 1 1 auto;
  min-height: 0;

  @media (max-width: 860px) {
    display: flex;
    flex-wrap: wrap;
    padding: 0.25rem 0;
    flex: 0 0 auto;
  }
`;

/* A 2px oxide edge marks where you are. No filled pill, no blue. */
const RailLink = styled(NavLink)`
  display: flex;
  align-items: center;
  gap: 0.7rem;
  /* min-height, not height: "Proxmox Mail Gateway" is the longest label in
     this rail and a fixed row height made it overflow its own box rather
     than wrap. A nav item has to survive a long name. */
  min-height: 2.5rem;
  padding: 0.4rem 1.25rem;
  font-size: 0.875rem;
  line-height: 1.3;
  color: rgba(244, 241, 234, 0.72);
  text-decoration: none;
  border-left: 2px solid transparent;
  transition:
    color ${theme.motion.fast} ease-out,
    background ${theme.motion.fast} ease-out;

  &:hover {
    color: ${theme.paper};
    background: rgba(255, 255, 255, 0.04);
  }

  &.active {
    color: ${theme.paper};
    border-left-color: ${theme.oxide};
    background: rgba(255, 255, 255, 0.06);
    font-weight: 600;
  }

  ${focusRingOnDark}
`;

const RailGroup = styled.div`
  border-top: 1px solid rgba(255, 255, 255, 0.1);
  padding: 0.35rem 0;
  display: flex;
  flex-shrink: 0;
`;

const RailFoot = styled.div`
  border-top: 1px solid rgba(255, 255, 255, 0.1);
  padding: 0.6rem;
  position: relative;
  flex-shrink: 0;
`;

const AccountButton = styled.button`
  width: 100%;
  display: flex;
  align-items: center;
  gap: 0.65rem;
  border: 0;
  background: transparent;
  cursor: pointer;
  font: inherit;
  text-align: left;
  padding: 0.4rem 0.6rem;
  border-radius: ${theme.radius.sm};
  color: ${theme.paper};
  transition: background ${theme.motion.fast} ease-out;

  &:hover {
    background: rgba(255, 255, 255, 0.05);
  }

  ${focusRingOnDark}
`;

const AccountMeta = styled.span`
  flex: 1 1 auto;
  min-width: 0;
  line-height: 1.25;

  strong {
    display: block;
    font-size: 0.83rem;
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  span {
    display: block;
    font-size: 0.72rem;
    color: rgba(244, 241, 234, 0.55);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
`;

const Main = styled.div`
  min-width: 0;
  display: flex;
  flex-direction: column;
`;

const HintSlot = styled.div`
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 0.5rem;
  padding: 0.5rem 1.75rem 0;
`;

/* Ink band with an ochre or oxide edge. It sits above the page rather than
   inside it, because it is true of the appliance and not of the screen. */
const LicenseBanner = styled.div<{ $expired?: boolean }>`
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 0.65rem;
  padding: 0.55rem 1.75rem;
  font-size: 0.83rem;
  line-height: 1.45;
  color: ${theme.paper};
  background: ${theme.ink};
  border-bottom: 2px solid ${(p) => (p.$expired ? theme.oxide : theme.warn)};

  i {
    width: 0.6rem;
    height: 0.6rem;
    flex-shrink: 0;
    background: ${(p) => (p.$expired ? theme.oxide : theme.warn)};
  }
`;

const BannerLink = styled(Link)`
  margin-left: auto;
  color: ${theme.paper};
  text-decoration: underline;
  white-space: nowrap;
`;

export function ConsoleChrome({
  hint,
  children,
  setupMode = false,
}: {
  subtitle?: string;
  hint?: ReactNode;
  children: ReactNode;
  /** Initial wizard setup: brand only; hide nav / account chrome until deploy is done. */
  setupMode?: boolean;
}) {
  const { user, logout } = useAuth();
  const { deployed } = useSetup();
  const navigate = useNavigate();
  const isSuper = user?.role === "kin_super_admin";
  // Support-Ops can run mail gateway operations too; the RBAC matrix puts
  // mail_gateway in SENSITIVE_OPS_COMMANDS, which is both ops roles.
  const isOps = isSuper || user?.role === "kin_support_ops";
  const [menuOpen, setMenuOpen] = useState(false);
  const alerts = useAlerts();
  const [license, setLicense] = useState<{
    status?: string;
    type?: string;
    grace_until?: string | null;
    provisioning_blocked?: boolean;
  } | null>(null);

  useEffect(() => {
    if (!user || setupMode) {
      setLicense(null);
      return;
    }
    let cancelled = false;
    void api<{ status?: string; type?: string; grace_until?: string | null; provisioning_blocked?: boolean }>(
      "/api/license/status",
    )
      .then((st) => {
        if (!cancelled) setLicense(st);
      })
      .catch(() => {
        if (!cancelled) setLicense(null);
      });
    return () => {
      cancelled = true;
    };
  }, [user, setupMode]);

  const railNav = (
    <>
      <RailLink to="/cluster" end>
        Cluster
      </RailLink>
      {!deployed ? <RailLink to="/wizard">Wizard</RailLink> : null}
      <RailLink to="/mailboxes" end>
        Mailboxes
      </RailLink>
      {/* Ops-level: the page repoints where every message this appliance sends
          and receives goes, so a Customer Admin has no business in it. The
          backend refuses them anyway; hiding it keeps the rail honest.

          Named in full. "Mail Gateway" reads like something we wrote and
          therefore support end to end; this is a Proxmox appliance the customer
          runs, and the difference matters the first time somebody opens a
          ticket about a spam rule. */}
      {isSuper || isOps ? (
        <RailLink to="/mail-gateway" end>
          Proxmox Mail Gateway
        </RailLink>
      ) : null}
      {isSuper ? (
        <RailLink to="/users" end>
          Users
        </RailLink>
      ) : null}
      {isSuper ? (
        <RailLink to="/settings" end>
          Settings
        </RailLink>
      ) : null}
    </>
  );

  if (setupMode) {
    return (
      <SetupFrame>
        <SetupTop>
          <BrandLockup compact />
        </SetupTop>
        {children}
      </SetupFrame>
    );
  }

  return (
    <Frame>
      {/* data-kin-rail is the hook the print stylesheet uses. A report handed
          to somebody should be the report, not a screenshot of the console
          with the navigation down the side of every page. */}
      <Rail aria-label="Console navigation" data-kin-rail="">
        <RailBrand type="button" onClick={() => navigate("/cluster")}>
          <BrandLockup compact light />
        </RailBrand>
        <RailNav>{railNav}</RailNav>
        {user ? (
          <RailGroup>
            <TaskCenter alerts={alerts} rail />
          </RailGroup>
        ) : null}
        {user ? (
          <RailFoot>
            <AccountButton
              type="button"
              onClick={() => setMenuOpen((v) => !v)}
              aria-haspopup="menu"
              aria-expanded={menuOpen}
            >
              <Avatar name={user.username} size={30} />
              <AccountMeta>
                <strong>{user.username}</strong>
                <span>{user.role_label || user.role}</span>
              </AccountMeta>
            </AccountButton>
            <Dropdown open={menuOpen} onClose={() => setMenuOpen(false)} align="left" drop="up">
              <MenuItem
                type="button"
                onClick={() => {
                  setMenuOpen(false);
                  navigate("/security");
                }}
              >
                Security
              </MenuItem>
              <MenuItem
                type="button"
                data-danger="true"
                onClick={() => {
                  setMenuOpen(false);
                  void logout().then(() => navigate("/login"));
                }}
              >
                Sign out
              </MenuItem>
            </Dropdown>
          </RailFoot>
        ) : null}
      </Rail>
      <Main>
        {license?.status === "grace" ? (
          <LicenseBanner>
            <i />
            License is in the 30-day grace period
            {license.grace_until ? ` (until ${license.grace_until})` : ""}. New mailboxes and
            console users cannot be created until a new license is applied. Mail already
            delivered keeps working.
            {isSuper ? <BannerLink to="/settings">Settings</BannerLink> : null}
          </LicenseBanner>
        ) : null}
        {license?.status === "expired" ? (
          <LicenseBanner $expired>
            <i />
            License has expired. New mailboxes and console users cannot be created. Mail
            already delivered keeps working.
            {isSuper ? <BannerLink to="/settings">Settings</BannerLink> : null}
          </LicenseBanner>
        ) : null}
        {hint ? <HintSlot>{hint}</HintSlot> : null}
        {children}
      </Main>
    </Frame>
  );
}
