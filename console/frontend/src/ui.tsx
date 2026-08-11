import styled from "@emotion/styled";
import { theme } from "./styles/theme";

export const Shell = styled.div`
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 2rem 1.25rem;
  background:
    radial-gradient(1100px 520px at 8% -12%, #1a2740 0%, transparent 55%),
    radial-gradient(900px 480px at 100% 0%, #15261f 0%, transparent 48%),
    ${theme.bg};
  color: ${theme.ink};
  font-family: ${theme.font};
`;

export const Card = styled.div`
  width: min(560px, 100%);
  background: color-mix(in srgb, ${theme.bgElev} 94%, black);
  border: 1px solid ${theme.line};
  border-radius: calc(${theme.radius} + 2px);
  padding: 1.75rem 1.5rem 1.5rem;
  box-shadow: ${theme.shadow};
`;

export const Brand = styled.p`
  font-size: 0.72rem;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: ${theme.muted};
  margin: 0 0 0.35rem;
`;

export const Title = styled.h1`
  margin: 0 0 0.4rem;
  font-size: 1.55rem;
  font-weight: 650;
  letter-spacing: -0.02em;
`;

export const Lede = styled.p`
  margin: 0 0 1.25rem;
  color: ${theme.muted};
  font-size: 0.95rem;
  line-height: 1.45;
`;

export const Label = styled.label`
  display: block;
  font-size: 0.8rem;
  color: ${theme.muted};
  margin: 0 0 0.35rem;
`;

export const Input = styled.input`
  width: 100%;
  border: 1px solid ${theme.line};
  background: ${theme.bgPanel};
  color: ${theme.ink};
  border-radius: ${theme.radius};
  padding: 0.7rem 0.8rem;
  font: inherit;
  margin-bottom: 0.9rem;

  &:focus {
    outline: 2px solid color-mix(in srgb, ${theme.accent} 55%, transparent);
    border-color: ${theme.accent};
  }
`;

export const TextArea = styled.textarea`
  width: 100%;
  border: 1px solid ${theme.line};
  background: ${theme.bgPanel};
  color: ${theme.ink};
  border-radius: ${theme.radius};
  padding: 0.7rem 0.8rem;
  font: inherit;
  margin-bottom: 0.9rem;
  min-height: 5rem;
  resize: vertical;

  &:focus {
    outline: 2px solid color-mix(in srgb, ${theme.accent} 55%, transparent);
    border-color: ${theme.accent};
  }
`;

export const Button = styled.button<{ variant?: "primary" | "ghost" | "danger" }>`
  border: ${(p) => (p.variant === "ghost" ? `1px solid ${theme.line}` : "0")};
  border-radius: ${theme.radius};
  background: ${(p) =>
    p.variant === "ghost"
      ? "transparent"
      : p.variant === "danger"
        ? theme.danger
        : theme.accent};
  color: ${(p) => (p.variant === "ghost" ? theme.ink : "#fff")};
  font: inherit;
  font-weight: 600;
  padding: 0.7rem 1rem;
  cursor: pointer;

  &:hover:not(:disabled) {
    background: ${(p) =>
      p.variant === "ghost"
        ? "rgba(255,255,255,0.04)"
        : p.variant === "danger"
          ? "#c03939"
          : theme.accentHover};
  }

  &:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }
`;

export const Err = styled.div`
  color: ${theme.danger};
  font-size: 0.85rem;
  margin: 0 0 0.85rem;
  min-height: 1.1em;
`;

export const Hint = styled.p`
  margin: -0.45rem 0 0.95rem;
  color: ${theme.muted};
  font-size: 0.82rem;
  line-height: 1.4;
`;

export const FieldRow = styled.div`
  display: grid;
  gap: 0.15rem;
`;

export const ChoiceGrid = styled.div`
  display: grid;
  gap: 0.75rem;
  margin-bottom: 1rem;
`;

export const Choice = styled.button<{ selected?: boolean }>`
  text-align: left;
  border: 1px solid ${(p) => (p.selected ? theme.accent : theme.line)};
  background: ${(p) => (p.selected ? theme.accentSoft : theme.bgPanel)};
  color: ${theme.ink};
  border-radius: ${theme.radius};
  padding: 0.95rem 1rem;
  cursor: pointer;
  font: inherit;

  strong {
    display: block;
    margin-bottom: 0.25rem;
  }

  span {
    color: ${theme.muted};
    font-size: 0.86rem;
    line-height: 1.4;
  }
`;

export const WarnBox = styled.div`
  border: 1px solid color-mix(in srgb, ${theme.warn} 45%, ${theme.line});
  background: ${theme.warnSoft};
  color: ${theme.ink};
  border-radius: ${theme.radius};
  padding: 0.85rem 1rem;
  margin-bottom: 1rem;
  font-size: 0.88rem;
  line-height: 1.45;

  strong {
    color: ${theme.warn};
  }
`;

export const NavRow = styled.div`
  display: flex;
  justify-content: space-between;
  gap: 0.75rem;
  margin-top: 1.25rem;
  padding-top: 1rem;
  border-top: 1px solid ${theme.line};
`;

export const CheckRow = styled.label`
  display: flex;
  align-items: flex-start;
  gap: 0.65rem;
  margin: 1rem 0 1.25rem;
  font-size: 0.92rem;
  line-height: 1.4;
  cursor: pointer;

  input {
    margin-top: 0.2rem;
  }
`;

export const Mono = styled.span`
  font-family: ${theme.mono};
  font-size: 0.85rem;
`;

export const SummaryTable = styled.dl`
  display: grid;
  grid-template-columns: 11rem 1fr;
  gap: 0.45rem 1rem;
  margin: 0 0 1rem;
  font-size: 0.9rem;

  dt {
    color: ${theme.muted};
  }

  dd {
    margin: 0;
    word-break: break-word;
  }
`;

export const LogPane = styled.pre`
  margin: 0 0 1rem;
  min-height: 220px;
  max-height: 360px;
  overflow: auto;
  background: #070a0e;
  border: 1px solid ${theme.line};
  border-radius: ${theme.radius};
  padding: 0.9rem 1rem;
  color: ${theme.muted};
  font-family: ${theme.mono};
  font-size: 0.8rem;
  line-height: 1.45;
  white-space: pre-wrap;
`;
