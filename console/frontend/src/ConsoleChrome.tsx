import { useEffect, useState, type ReactNode } from "react";
import styled from "@emotion/styled";
import { NavLink, useNavigate } from "react-router-dom";
import { api } from "./api";
import { useAuth } from "./auth";
import { useSetup } from "./setup";
import { theme } from "./styles/theme";
import { Avatar, BrandLockup, Dropdown, MenuItem } from "./ui";

const Frame = styled.div`
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  background: ${theme.bg};
  color: ${theme.ink};
  font-family: ${theme.font};
`;

const Top = styled.header`
  min-height: 3.5rem;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 0.65rem 1rem;
  padding: 0.55rem 1.5rem;
  border-bottom: 1px solid color-mix(in srgb, ${theme.line} 60%, transparent);
  background: ${theme.bgElev};
  flex-shrink: 0;
`;

const BrandBlock = styled.div`
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.75rem;
  min-width: 0;
  flex: 1 1 12rem;
`;

const LogoButton = styled.button`
  border: 0;
  background: transparent;
  padding: 0;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  justify-content: center;
  gap: 0.1rem;
  font: inherit;
  min-width: 0;

  span {
    color: ${theme.muted};
    font-size: 0.68rem;
    font-weight: 500;
    line-height: 1;
  }
`;

const Divider = styled.div`
  width: 1px;
  height: 1.75rem;
  background: ${theme.line};
  flex-shrink: 0;
`;

const NavLinks = styled.nav`
  display: flex;
  align-items: center;
  gap: 0.25rem;
  flex-wrap: wrap;
  flex: 1 1 16rem;
`;

const NavItem = styled(NavLink)`
  color: ${theme.surface[600]};
  text-decoration: none;
  font-size: 0.875rem;
  font-weight: 500;
  padding: 0.35rem 0.75rem;
  border-radius: ${theme.radius.md};
  border: 1px solid transparent;
  transition:
    color ${theme.motion.fast} ease-out,
    background ${theme.motion.fast} ease-out,
    border-color ${theme.motion.fast} ease-out;

  &:hover {
    background: ${theme.surface[50]};
    color: ${theme.surface[800]};
  }

  &.active {
    background: color-mix(in srgb, ${theme.accent} 6%, transparent);
    border-color: color-mix(in srgb, ${theme.accent} 20%, transparent);
    color: ${theme.accent};
  }
`;

const Right = styled.div`
  display: flex;
  align-items: center;
  gap: 0.5rem;
  position: relative;
`;

const UserButton = styled.button`
  display: flex;
  align-items: center;
  gap: 0.75rem;
  border: 0;
  background: transparent;
  cursor: pointer;
  font: inherit;
  padding: 0.25rem 0.5rem 0.25rem 1rem;
  margin-right: -0.5rem;
  border-left: 1px solid ${theme.line};
  border-radius: ${theme.radius.md};
  transition: background ${theme.motion.fast} ease-out;

  &:hover {
    background: ${theme.surface[50]};
  }
`;

const UserMeta = styled.div`
  text-align: right;
  line-height: 1.2;

  strong {
    display: block;
    font-size: 0.875rem;
    font-weight: 600;
    color: ${theme.surface[800]};
  }

  span {
    display: block;
    font-size: 11px;
    color: ${theme.surface[400]};
  }
`;

const LicenseBanner = styled.div<{ $expired?: boolean }>`
  flex-shrink: 0;
  padding: 0.7rem 1.5rem;
  font-size: 0.88rem;
  line-height: 1.45;
  color: ${theme.ink};
  background: ${(p) => (p.$expired ? "rgba(239, 68, 68, 0.12)" : theme.warnSoft)};
  border-bottom: 1px solid
    ${(p) =>
      p.$expired
        ? "color-mix(in srgb, " + theme.danger + " 35%, transparent)"
        : "color-mix(in srgb, " + theme.warn + " 35%, transparent)"};
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
  const [menuOpen, setMenuOpen] = useState(false);
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

  return (
    <Frame>
      <Top>
        <BrandBlock>
          <LogoButton type="button" onClick={() => navigate(setupMode ? "/wizard" : "/cluster")}>
            <BrandLockup compact />
          </LogoButton>
          {!setupMode ? (
            <>
              <Divider />
              <NavLinks>
                <NavItem to="/cluster" end>
                  Cluster
                </NavItem>
                {!deployed && <NavItem to="/wizard">Wizard</NavItem>}
                <NavItem to="/mailboxes" end>
                  Mailboxes
                </NavItem>
                {isSuper && (
                  <NavItem to="/users" end>
                    Users
                  </NavItem>
                )}
                {isSuper && (
                  <NavItem to="/settings" end>
                    Settings
                  </NavItem>
                )}
              </NavLinks>
            </>
          ) : null}
        </BrandBlock>
        {!setupMode ? (
          <Right>
            {hint}
            {user ? (
              <>
                <UserButton
                  type="button"
                  onClick={() => setMenuOpen((v) => !v)}
                  aria-haspopup="menu"
                  aria-expanded={menuOpen}
                >
                  <UserMeta>
                    <strong>{user.username}</strong>
                    <span>{user.role_label || user.role}</span>
                  </UserMeta>
                  <Avatar name={user.username} size={36} />
                </UserButton>
                <Dropdown open={menuOpen} onClose={() => setMenuOpen(false)} align="right">
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
                    <svg fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                      <path
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        d="M15.75 9V5.25A2.25 2.25 0 0013.5 3h-6a2.25 2.25 0 00-2.25 2.25v13.5A2.25 2.25 0 007.5 21h6a2.25 2.25 0 002.25-2.25V15m3 0l3-3m0 0l-3-3m3 3H9"
                      />
                    </svg>
                    Sign Out
                  </MenuItem>
                </Dropdown>
              </>
            ) : null}
          </Right>
        ) : null}
      </Top>
      {license?.status === "grace" ? (
        <LicenseBanner>
          License is in the 30-day grace period
          {license.grace_until ? ` (until ${license.grace_until})` : ""}. New mailboxes and
          console users cannot be created until a new license is applied. Mail already delivered
          keeps working.
        </LicenseBanner>
      ) : null}
      {license?.status === "expired" ? (
        <LicenseBanner $expired>
          License has expired. New mailboxes and console users cannot be created. Mail
          already delivered keeps working. Apply a current license in Settings.
        </LicenseBanner>
      ) : null}
      {children}
    </Frame>
  );
}
