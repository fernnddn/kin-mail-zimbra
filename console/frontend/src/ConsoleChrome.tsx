import type { ReactNode } from "react";
import styled from "@emotion/styled";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "./auth";
import { theme } from "./styles/theme";
import { Button, Mono } from "./ui";

const Frame = styled.div`
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  background: ${theme.bg};
  color: ${theme.ink};
  font-family: ${theme.font};
`;

const Top = styled.header`
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  padding: 0.85rem 1.25rem;
  border-bottom: 1px solid ${theme.line};
  background: color-mix(in srgb, ${theme.bgElev} 92%, transparent);
`;

const BrandMark = styled.div`
  display: flex;
  flex-direction: column;
  gap: 0.1rem;

  strong {
    letter-spacing: -0.02em;
    font-size: 1rem;
  }

  span {
    color: ${theme.muted};
    font-size: 0.75rem;
  }
`;

const NavLinks = styled.nav`
  display: flex;
  align-items: center;
  gap: 0.75rem;
  flex-wrap: wrap;

  a {
    color: ${theme.muted};
    text-decoration: none;
    font-size: 0.85rem;
    transition: color ${theme.motion} ease-out;
  }

  a:hover {
    color: ${theme.ink};
  }
`;

export function ConsoleChrome({
  subtitle,
  hint,
  children,
  setupMode = false,
}: {
  subtitle?: string;
  hint?: ReactNode;
  children: ReactNode;
  /** Initial wizard setup: brand only — hide nav / account chrome until deploy is done. */
  setupMode?: boolean;
}) {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const isSuper = user?.role === "kin_super_admin";

  return (
    <Frame>
      <Top>
        <BrandMark>
          <strong>KIN Mail Console</strong>
          {!setupMode && subtitle ? <span>{subtitle}</span> : null}
        </BrandMark>
        {!setupMode ? (
          <div style={{ display: "flex", alignItems: "center", gap: "0.85rem", flexWrap: "wrap" }}>
            {hint}
            <NavLinks>
              <Link to="/wizard">Wizard</Link>
              <Link to="/mailboxes">Mailboxes</Link>
              {isSuper && <Link to="/users">Users</Link>}
              {isSuper && <Link to="/audit">Audit log</Link>}
            </NavLinks>
            <Mono style={{ color: theme.muted }}>
              {user?.username}
              {user?.role_label ? ` · ${user.role_label}` : ""}
            </Mono>
            <Button
              type="button"
              variant="ghost"
              style={{ padding: "0.4rem 0.75rem", fontSize: "0.85rem" }}
              onClick={() => void logout().then(() => navigate("/login"))}
            >
              Sign out
            </Button>
          </div>
        ) : null}
      </Top>
      {children}
    </Frame>
  );
}
