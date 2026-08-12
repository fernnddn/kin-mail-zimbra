import type { ReactNode } from "react";
import styled from "@emotion/styled";
import { keyframes } from "@emotion/react";
import { theme } from "./styles/theme";

export const Shell = styled.div`
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 2rem 1.25rem;
  background:
    radial-gradient(1100px 520px at 8% -12%, #d9e8ff 0%, transparent 55%),
    radial-gradient(900px 480px at 100% 0%, #e8eef8 0%, transparent 48%),
    ${theme.bg};
  color: ${theme.ink};
  font-family: ${theme.font};
`;

export const Card = styled.div`
  width: min(560px, 100%);
  background: ${theme.bgElev};
  border: 1px solid ${theme.line};
  border-radius: calc(${theme.radius} + 2px);
  padding: 1.75rem 1.5rem 1.5rem;
  box-shadow: ${theme.shadow};
  animation: kin-scale-in ${theme.motion} ease-out;
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

export const RequiredMark = styled.span`
  color: ${theme.danger};
  margin-left: 0.2rem;
  font-weight: 700;
`;

export const OptionalMark = styled.span`
  color: ${theme.muted};
  margin-left: 0.35rem;
  font-weight: 500;
  font-size: 0.78rem;
`;

export const Input = styled.input`
  width: 100%;
  border: 1px solid ${theme.line};
  background: ${theme.bgElev};
  color: ${theme.ink};
  border-radius: ${theme.radius};
  padding: 0.7rem 0.8rem;
  font: inherit;
  margin-bottom: 0.9rem;
  transition:
    border-color ${theme.motion} ease-out,
    box-shadow ${theme.motion} ease-out,
    background ${theme.motion} ease-out;

  &:focus {
    outline: 2px solid color-mix(in srgb, ${theme.accent} 35%, transparent);
    border-color: ${theme.accent};
  }
`;

export const TextArea = styled.textarea`
  width: 100%;
  border: 1px solid ${theme.line};
  background: ${theme.bgElev};
  color: ${theme.ink};
  border-radius: ${theme.radius};
  padding: 0.7rem 0.8rem;
  font: inherit;
  margin-bottom: 0.9rem;
  min-height: 5rem;
  resize: vertical;
  transition:
    border-color ${theme.motion} ease-out,
    box-shadow ${theme.motion} ease-out;

  &:focus {
    outline: 2px solid color-mix(in srgb, ${theme.accent} 35%, transparent);
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
  transition:
    background ${theme.motion} ease-out,
    border-color ${theme.motion} ease-out,
    transform ${theme.motion} ease-out,
    box-shadow ${theme.motion} ease-out;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 0.45rem;

  &:hover:not(:disabled) {
    background: ${(p) =>
      p.variant === "ghost"
        ? theme.bgPanel
        : p.variant === "danger"
          ? "#dc2626"
          : theme.accentHover};
    transform: translateY(-1px);
    box-shadow: 0 8px 20px rgba(15, 23, 42, 0.1);
  }

  &:disabled {
    opacity: 0.55;
    cursor: not-allowed;
    transform: none;
  }
`;

export const Err = styled.div`
  color: ${theme.danger};
  font-size: 0.85rem;
  margin: 0 0 0.85rem;
  min-height: 1.1em;
`;

export const OkMsg = styled.div`
  color: ${theme.ok};
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
  background: ${(p) => (p.selected ? theme.accentSoft : theme.bgElev)};
  color: ${theme.ink};
  border-radius: ${theme.radius};
  padding: 0.95rem 1rem;
  cursor: pointer;
  font: inherit;
  transition:
    border-color ${theme.motion} ease-out,
    background ${theme.motion} ease-out,
    box-shadow ${theme.motion} ease-out;

  &:hover {
    border-color: ${(p) => (p.selected ? theme.accent : theme.accentHover)};
    box-shadow: 0 4px 14px rgba(0, 97, 255, 0.08);
  }

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
  border: 1px solid color-mix(in srgb, ${theme.warn} 35%, ${theme.line});
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
  background: #0f172a;
  border: 1px solid ${theme.line};
  border-radius: ${theme.radius};
  padding: 0.9rem 1rem;
  color: #cbd5e1;
  font-family: ${theme.mono};
  font-size: 0.8rem;
  line-height: 1.45;
  white-space: pre-wrap;
`;

const spin = keyframes`
  to { transform: rotate(360deg); }
`;

export const Spinner = styled.span`
  width: 0.95rem;
  height: 0.95rem;
  border: 2px solid color-mix(in srgb, currentColor 28%, transparent);
  border-top-color: currentColor;
  border-radius: 999px;
  display: inline-block;
  animation: ${spin} 0.7s linear infinite;
  flex-shrink: 0;
`;

export function FieldLabel({
  htmlFor,
  children,
  required,
  optional,
}: {
  htmlFor?: string;
  children: ReactNode;
  required?: boolean;
  optional?: boolean;
}) {
  return (
    <Label htmlFor={htmlFor}>
      {children}
      {required ? <RequiredMark aria-hidden="true">*</RequiredMark> : null}
      {optional ? <OptionalMark>(optional)</OptionalMark> : null}
    </Label>
  );
}
