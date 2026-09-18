import { useEffect, useRef, useState, type ReactNode } from "react";
import styled from "@emotion/styled";
import { Link, NavLink, useLocation, useNavigate } from "react-router-dom";
import { api } from "./api";
import { useAuth } from "./auth";
import { prefersReducedMotion, slideTo } from "./lib/motion";
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

/* Travels between nav items rather than blinking from one to the next. Sits
   behind the links, so it can never intercept a click. */
const RailMarker = styled.span`
  position: absolute;
  left: 0;
  width: 2px;
  background: ${theme.oxide};
  border-radius: 0 2px 2px 0;
  pointer-events: none;
  opacity: 0;
`;

/* Takes the space the nav does not use, so the alert bell and the account
   button stay at the BOTTOM of the rail.

   RailNav already carried `flex: 1 1 auto` for that, but it stopped working
   the moment the nav was wrapped to host the sliding marker: the wrapper is
   the flex child of the rail now, and it sized to its content, so everything
   below it bunched up directly under the last nav item. The growth belongs to
   whichever element the rail actually lays out. */
const RailNavWrap = styled.div`
  position: relative;
  flex: 1 1 auto;
  min-height: 0;

  @media (max-width: 860px) {
    flex: 0 0 auto;
  }
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

  /* The 2px edge stays as the accessible, no-JS marker: if the travelling
     indicator below never runs - reduced motion, a failed measure - the
     active item is still unambiguous. */
  &.active {
    color: ${theme.paper};
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

/**
 * Slide the rail marker to whichever nav item is active.
 *
 * Measured from the DOM rather than tracked in state, because the rail is a
 * plain list of NavLinks and the active one is decided by the router. A
 * measurement that fails - the item is not laid out yet, the rail is a
 * horizontal band on a narrow screen - simply leaves the marker hidden, and
 * the `.active` background still says where you are.
 */
function useRailMarker(
  navRef: React.RefObject<HTMLDivElement | null>,
  markerRef: React.RefObject<HTMLSpanElement | null>,
  pathname: string,
) {
  useEffect(() => {
    const host = navRef.current;
    const marker = markerRef.current;
    if (!host || !marker) return;
    // Two frames: one for the router to apply .active, one for layout.
    const id = window.requestAnimationFrame(() =>
      window.requestAnimationFrame(() => {
        const active = host.querySelector<HTMLElement>("a.active");
        if (!active) {
          marker.style.opacity = "0";
          return;
        }
        const top = active.offsetTop;
        const height = active.offsetHeight;
        if (!height) {
          marker.style.opacity = "0";
          return;
        }
        const first = marker.style.opacity !== "1";
        marker.style.opacity = "1";
        if (first || prefersReducedMotion()) {
          marker.style.top = `${top}px`;
          marker.style.height = `${height}px`;
          return;
        }
        slideTo(marker, top, height);
      }),
    );
    return () => window.cancelAnimationFrame(id);
  }, [pathname, navRef, markerRef]);
}

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
  const navRef = useRef<HTMLDivElement | null>(null);
  const markerRef = useRef<HTMLSpanElement | null>(null);
  /* useLocation, not window.location: the latter compiles fine and is not
     reactive, so the marker would only move on a full page load and would sit
     under the wrong item for the whole of a normal session. */
  const routerLocation = useLocation();
  useRailMarker(navRef, markerRef, routerLocation.pathname);
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
        <RailNavWrap ref={navRef}>
          <RailMarker ref={markerRef} aria-hidden="true" />
          <RailNav>{railNav}</RailNav>
        </RailNavWrap>
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
