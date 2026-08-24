import {
  useEffect,
  useId,
  useState,
  type ButtonHTMLAttributes,
  type ComponentProps,
  type CSSProperties,
  type ImgHTMLAttributes,
  type ReactNode,
} from "react";
import styled from "@emotion/styled";
import { keyframes } from "@emotion/react";
import { theme } from "./styles/theme";
import kinMailLogo from "./assets/kinmail-lockup-black.png";

const spin = keyframes`
  to { transform: rotate(360deg); }
`;

const scaleIn = keyframes`
  from { opacity: 0; transform: scale(0.95); }
  to { opacity: 1; transform: scale(1); }
`;

const pulse = keyframes`
  0%, 100% { opacity: 1; }
  50% { opacity: 0.45; }
`;

export const Shell = styled.div`
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 2rem 1.25rem;
  position: relative;
  overflow: hidden;
  background:
    radial-gradient(1100px 520px at 8% -12%, ${theme.primary[100]} 0%, transparent 55%),
    radial-gradient(900px 480px at 100% 0%, ${theme.surface[100]} 0%, transparent 48%),
    linear-gradient(180deg, #f0f4fa 0%, #f7f9fc 48%, #ffffff 100%);
  color: ${theme.ink};
  font-family: ${theme.font};

  &::before {
    content: "";
    position: absolute;
    inset: 0;
    pointer-events: none;
    opacity: 0.12;
    background-image: radial-gradient(circle, ${theme.accent} 1px, transparent 1px);
    background-size: 28px 28px;
  }

  > * {
    position: relative;
    z-index: 1;
  }
`;

export const Card = styled.div`
  width: min(560px, 100%);
  background: ${theme.bgElev};
  border: 1px solid ${theme.line};
  border-radius: ${theme.radius.xl};
  padding: 1.75rem 1.5rem 1.5rem;
  box-shadow: ${theme.shadow.sm};
  animation: kin-scale-in ${theme.motion.fast} ease-out;
`;

export const Brand = styled.p`
  font-size: 0.72rem;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: ${theme.muted};
  margin: 0 0 0.35rem;
`;

const LockupImg = styled.img<{ $compact?: boolean }>`
  display: block;
  height: ${(p) => (p.$compact ? "2.4rem" : "4.1rem")};
  width: auto;
  max-width: ${(p) => (p.$compact ? "11rem" : "16.5rem")};
  object-fit: contain;
  object-position: left top;
`;

const CompactLockup = styled.span`
  display: block;
  height: 2.4rem;
  overflow: hidden;
  line-height: 0;
`;

/** Official KIN Mail lockup. Compact height matches the image so the bar does not clip it. */
export function BrandLockup({
  compact = false,
  alt = "KIN Mail",
  ...rest
}: { compact?: boolean } & ImgHTMLAttributes<HTMLImageElement>) {
  const img = <LockupImg src={kinMailLogo} alt={alt} $compact={compact} {...rest} />;
  if (compact) return <CompactLockup>{img}</CompactLockup>;
  return img;
}

export const Title = styled.h1`
  margin: 0 0 0.4rem;
  font-size: 1.25rem;
  font-weight: 700;
  letter-spacing: -0.02em;
  color: ${theme.surface[800]};
`;

export const Lede = styled.p`
  margin: 0 0 1.25rem;
  color: ${theme.muted};
  font-size: 0.9rem;
  line-height: 1.5;
`;

export const Label = styled.label`
  display: block;
  font-size: 0.875rem;
  font-weight: 500;
  color: ${theme.surface[700]};
  margin: 0 0 0.4rem;
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

const focusRing = `
  &:focus {
    outline: none;
    border-color: ${theme.accent};
    box-shadow: 0 0 0 3px color-mix(in srgb, ${theme.accent} 10%, transparent);
  }
  &:focus-visible {
    outline: none;
    border-color: ${theme.accent};
    box-shadow: 0 0 0 3px color-mix(in srgb, ${theme.accent} 10%, transparent);
  }
`;

export const Input = styled.input`
  width: 100%;
  border: 1px solid ${theme.surface[300]};
  background: ${theme.bgElev};
  color: ${theme.ink};
  border-radius: ${theme.radius.md};
  padding: 0.625rem 1rem;
  font: inherit;
  font-size: 0.875rem;
  margin-bottom: 0.9rem;
  transition:
    border-color ${theme.motion.fast} ease-out,
    box-shadow ${theme.motion.fast} ease-out,
    background ${theme.motion.fast} ease-out;

  &::placeholder {
    color: ${theme.surface[400]};
  }

  &:disabled {
    opacity: 0.5;
  }

  ${focusRing}
`;

const PasswordWrap = styled.div`
  position: relative;
  margin-bottom: 0.9rem;

  ${Input} {
    margin-bottom: 0;
    padding-right: 2.75rem;
  }
`;

const PasswordToggle = styled.button`
  position: absolute;
  top: 50%;
  right: 0.35rem;
  transform: translateY(-50%);
  border: 0;
  background: transparent;
  color: ${theme.surface[400]};
  cursor: pointer;
  width: 2.1rem;
  height: 2.1rem;
  display: grid;
  place-items: center;
  border-radius: ${theme.radius.sm};
  padding: 0;
  transition:
    color ${theme.motion.fast} ease-out,
    background ${theme.motion.fast} ease-out;

  &:hover {
    color: ${theme.surface[700]};
    background: ${theme.surface[50]};
  }

  &:focus-visible {
    outline: 2px solid color-mix(in srgb, ${theme.accent} 35%, transparent);
  }
`;

function EyeIcon({ open }: { open: boolean }) {
  if (open) {
    return (
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path
          d="M3.98 8.223A10.477 10.477 0 001.934 12C3.226 16.338 7.244 19.5 12 19.5c.993 0 1.953-.138 2.863-.395M6.228 6.228A10.45 10.45 0 0112 4.5c4.756 0 8.773 3.162 10.065 7.498a10.523 10.523 0 01-4.293 5.774M6.228 6.228L3 3m3.228 3.228l3.65 3.65m7.894 7.894L21 21m-3.228-3.228l-3.65-3.65m0 0a3 3 0 10-4.243-4.243m4.242 4.242L9.88 9.88"
          stroke="currentColor"
          strokeWidth="1.75"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    );
  }
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M2.036 12.322a1.012 1.012 0 010-.639C3.423 7.51 7.36 4.5 12 4.5c4.638 0 8.573 3.007 9.963 7.178.07.207.07.431 0 .639C20.577 16.49 16.64 19.5 12 19.5c-4.638 0-8.573-3.007-9.963-7.178z"
        stroke="currentColor"
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"
        stroke="currentColor"
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** Password field with show/hide toggle. Paste/copy are intentionally allowed. */
export function PasswordInput({
  className,
  style,
  ...props
}: Omit<ComponentProps<"input">, "type">) {
  const [visible, setVisible] = useState(false);
  return (
    <PasswordWrap className={className} style={style}>
      <Input {...props} type={visible ? "text" : "password"} />
      <PasswordToggle
        type="button"
        tabIndex={-1}
        aria-label={visible ? "Hide password" : "Show password"}
        aria-pressed={visible}
        onClick={() => setVisible((v) => !v)}
      >
        <EyeIcon open={visible} />
      </PasswordToggle>
    </PasswordWrap>
  );
}

export const TextArea = styled.textarea`
  width: 100%;
  border: 1px solid ${theme.surface[300]};
  background: ${theme.bgElev};
  color: ${theme.ink};
  border-radius: ${theme.radius.md};
  padding: 0.625rem 1rem;
  font: inherit;
  font-size: 0.875rem;
  margin-bottom: 0.9rem;
  min-height: 5rem;
  resize: vertical;
  transition:
    border-color ${theme.motion.fast} ease-out,
    box-shadow ${theme.motion.fast} ease-out;

  ${focusRing}
`;

export const Select = styled.select`
  width: 100%;
  border: 1px solid ${theme.surface[300]};
  background: ${theme.bgElev};
  color: ${theme.ink};
  border-radius: ${theme.radius.md};
  padding: 0.625rem 1rem;
  font: inherit;
  font-size: 0.875rem;
  margin-bottom: 0.9rem;
  transition:
    border-color ${theme.motion.fast} ease-out,
    box-shadow ${theme.motion.fast} ease-out;

  ${focusRing}
`;

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger" | "destructive";

const ButtonRoot = styled.button<{ $variant: ButtonVariant }>`
  border: ${(p) =>
    p.$variant === "secondary"
      ? `1px solid ${theme.surface[200]}`
      : p.$variant === "ghost"
        ? "1px solid transparent"
        : "0"};
  border-radius: ${theme.radius.md};
  background: ${(p) =>
    p.$variant === "ghost" || p.$variant === "secondary"
      ? "transparent"
      : p.$variant === "danger" || p.$variant === "destructive"
        ? theme.danger
        : theme.accent};
  color: ${(p) =>
    p.$variant === "ghost" || p.$variant === "secondary" ? theme.ink : "#fff"};
  font: inherit;
  font-size: 0.875rem;
  font-weight: 600;
  padding: 0.625rem 1rem;
  cursor: pointer;
  box-shadow: ${(p) => (p.$variant === "primary" ? theme.shadow.sm : "none")};
  transition:
    background ${theme.motion.fast} ease-out,
    border-color ${theme.motion.fast} ease-out,
    color ${theme.motion.fast} ease-out,
    box-shadow ${theme.motion.fast} ease-out,
    transform ${theme.motion.fast} ease-out;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 0.45rem;

  &:hover:not(:disabled) {
    background: ${(p) =>
      p.$variant === "ghost"
        ? theme.surface[50]
        : p.$variant === "secondary"
          ? theme.surface[50]
          : p.$variant === "danger" || p.$variant === "destructive"
            ? "#dc2626"
            : theme.primary[700]};
    box-shadow: ${(p) => (p.$variant === "primary" ? theme.shadow.md : "none")};
  }

  &:active:not(:disabled) {
    transform: scale(0.98);
  }

  &:disabled {
    opacity: 1;
    cursor: not-allowed;
    transform: none;
    box-shadow: none;
    background: ${theme.surface[100]};
    color: ${theme.surface[400]};
    border: 1px solid ${theme.surface[200]};
  }
`;

export const Spinner = styled.span<{ $size?: number }>`
  width: ${(p) => (p.$size ?? 14) / 16}rem;
  height: ${(p) => (p.$size ?? 14) / 16}rem;
  border: 2px solid color-mix(in srgb, currentColor 30%, transparent);
  border-top-color: currentColor;
  border-radius: ${theme.radius.full};
  display: inline-block;
  animation: ${spin} 0.7s linear infinite;
  flex-shrink: 0;
`;

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant;
  loading?: boolean;
};

export function Button({
  variant = "primary",
  loading = false,
  disabled,
  children,
  ...props
}: ButtonProps) {
  return (
    <ButtonRoot $variant={variant} disabled={disabled || loading} {...props}>
      {loading ? <Spinner /> : null}
      {children}
    </ButtonRoot>
  );
}

const ErrBox = styled.div`
  color: #cf1322;
  font-size: 0.875rem;
  margin: 0 0 0.85rem;
  padding: 0.625rem 1rem;
  background: #fff1f0;
  border: 1px solid #ffa39e;
  border-radius: ${theme.radius.md};
  display: flex;
  align-items: flex-start;
  gap: 0.5rem;
`;

export function Err({
  children,
  className,
  style,
}: {
  children?: ReactNode;
  className?: string;
  style?: CSSProperties;
}) {
  if (children == null || children === false || children === "") return null;
  return (
    <ErrBox className={className} style={style}>
      {children}
    </ErrBox>
  );
}

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

/** Two fields side by side (password + confirm). Stacks on narrow screens. */
export const PairGrid = styled.div`
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0.35rem 1rem;
  margin-bottom: 0.5rem;

  @media (max-width: 640px) {
    grid-template-columns: 1fr;
  }
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
  border-radius: ${theme.radius.md};
  padding: 0.95rem 1rem;
  cursor: pointer;
  font: inherit;
  box-shadow: ${theme.shadow.sm};
  transition:
    border-color ${theme.motion.fast} ease-out,
    background ${theme.motion.fast} ease-out,
    box-shadow ${theme.motion.fast} ease-out;

  &:hover {
    border-color: ${(p) => (p.selected ? theme.accent : theme.accentHover)};
    box-shadow: ${theme.shadow.md};
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
  border-radius: ${theme.radius.md};
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
  background: ${theme.surface[900]};
  border: 1px solid ${theme.line};
  border-radius: ${theme.radius.md};
  padding: 0.9rem 1rem;
  color: ${theme.surface[300]};
  font-family: ${theme.mono};
  font-size: 0.8rem;
  line-height: 1.45;
  white-space: pre-wrap;
  box-shadow: ${theme.shadow.sm};
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

export const Page = styled.div`
  padding: 1.5rem 1.5rem 2rem;
  max-width: 960px;
  width: 100%;
  margin: 0 auto;
  animation: kin-page-in ${theme.motion.page} cubic-bezier(0.16, 1, 0.3, 1);
`;

const HeaderRow = styled.div`
  display: flex;
  align-items: center;
  gap: 1rem;
  margin-bottom: 1.25rem;
`;

const IconTile = styled.div<{ $from?: string; $to?: string }>`
  width: 3.5rem;
  height: 3.5rem;
  border-radius: ${theme.radius.xl};
  background: linear-gradient(
    135deg,
    ${(p) => p.$from || theme.accent} 0%,
    ${(p) => p.$to || theme.primary[800]} 100%
  );
  color: #fff;
  display: grid;
  place-items: center;
  flex-shrink: 0;
  box-shadow: ${theme.shadow.lg}, 0 8px 20px color-mix(in srgb, ${theme.accent} 25%, transparent);
`;

const HeaderText = styled.div`
  min-width: 0;
  flex: 1;

  h1 {
    margin: 0;
    font-size: 1.25rem;
    font-weight: 700;
    letter-spacing: -0.02em;
    color: ${theme.surface[800]};
  }

  p {
    margin: 0.25rem 0 0;
    font-size: 0.875rem;
    color: ${theme.muted};
    line-height: 1.45;
  }
`;

export function PageHeader({
  icon,
  title,
  subtitle,
  action,
  gradient,
}: {
  icon?: ReactNode;
  title: ReactNode;
  subtitle?: ReactNode;
  action?: ReactNode;
  gradient?: [string, string];
}) {
  return (
    <HeaderRow>
      {icon ? (
        <IconTile $from={gradient?.[0]} $to={gradient?.[1]}>
          {icon}
        </IconTile>
      ) : null}
      <HeaderText>
        <h1>{title}</h1>
        {subtitle ? <p>{subtitle}</p> : null}
      </HeaderText>
      {action}
    </HeaderRow>
  );
}

const Track = styled.button<{ $on: boolean }>`
  width: 48px;
  height: 28px;
  border: 0;
  padding: 0;
  border-radius: ${theme.radius.full};
  background: ${(p) => (p.$on ? theme.accent : theme.surface[300])};
  position: relative;
  cursor: pointer;
  flex-shrink: 0;
  transition: background-color ${theme.motion.base} ease-out;

  &:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  &:focus-visible {
    outline: 2px solid color-mix(in srgb, ${theme.accent} 45%, transparent);
    outline-offset: 2px;
  }
`;

const Thumb = styled.span<{ $on: boolean }>`
  position: absolute;
  top: 4px;
  left: 4px;
  width: 20px;
  height: 20px;
  border-radius: ${theme.radius.full};
  background: #fff;
  box-shadow: ${theme.shadow.sm};
  transform: translateX(${(p) => (p.$on ? "20px" : "0")});
  transition: transform ${theme.motion.base} ease-out;
`;

export function Switch({
  checked,
  onChange,
  disabled,
  label,
  id,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
  label?: ReactNode;
  id?: string;
}) {
  const autoId = useId();
  const switchId = id || autoId;
  const control = (
    <Track
      id={switchId}
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={typeof label === "string" ? label : undefined}
      $on={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
    >
      <Thumb $on={checked} />
    </Track>
  );
  if (!label) return control;
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: "0.65rem",
        margin: "1rem 0 1.25rem",
        fontSize: "0.92rem",
        lineHeight: 1.4,
      }}
    >
      {control}
      <label htmlFor={switchId} style={{ cursor: disabled ? "not-allowed" : "pointer" }}>
        {label}
      </label>
    </div>
  );
}

export type StatusTone =
  | "critical"
  | "high"
  | "medium"
  | "low"
  | "completed"
  | "neutral"
  | "primary"
  | "warn";

const TONE: Record<StatusTone, { fg: string; bg: string; border: string }> = {
  critical: { fg: theme.critical, bg: "rgba(239, 68, 68, 0.10)", border: "rgba(239, 68, 68, 0.20)" },
  high: { fg: theme.high, bg: "rgba(249, 115, 22, 0.10)", border: "rgba(249, 115, 22, 0.20)" },
  medium: { fg: "#a16207", bg: "rgba(234, 179, 8, 0.12)", border: "rgba(234, 179, 8, 0.28)" },
  low: { fg: theme.low, bg: "rgba(34, 197, 94, 0.10)", border: "rgba(34, 197, 94, 0.20)" },
  completed: { fg: "#047857", bg: "rgba(16, 185, 129, 0.10)", border: "rgba(16, 185, 129, 0.20)" },
  primary: { fg: theme.accent, bg: theme.accentSoft, border: "rgba(0, 97, 255, 0.20)" },
  warn: { fg: theme.warn, bg: theme.warnSoft, border: "rgba(217, 119, 6, 0.28)" },
  neutral: { fg: theme.surface[500], bg: theme.surface[100], border: theme.surface[200] },
};

const Pill = styled.span<{ $tone: StatusTone; $size: "tag" | "pill" }>`
  display: inline-flex;
  align-items: center;
  font-weight: 600;
  line-height: 1.2;
  color: ${(p) => TONE[p.$tone].fg};
  background: ${(p) => TONE[p.$tone].bg};
  border: 1px solid ${(p) => (p.$size === "pill" ? TONE[p.$tone].border : "transparent")};
  border-radius: ${(p) => (p.$size === "pill" ? theme.radius.full : theme.radius.sm)};
  font-size: ${(p) => (p.$size === "tag" ? "10px" : "11px")};
  padding: ${(p) => (p.$size === "tag" ? "0.1rem 0.4rem" : "0.15rem 0.5rem")};
  white-space: nowrap;
`;

export function StatusPill({
  tone = "neutral",
  size = "pill",
  children,
}: {
  tone?: StatusTone;
  size?: "tag" | "pill";
  children: ReactNode;
}) {
  return (
    <Pill $tone={tone} $size={size}>
      {children}
    </Pill>
  );
}

export const Badge = StatusPill;

export const Skeleton = styled.div<{ $h?: string; $w?: string }>`
  height: ${(p) => p.$h || "0.75rem"};
  width: ${(p) => p.$w || "100%"};
  border-radius: ${theme.radius.sm};
  background: ${theme.surface[100]};
  animation: ${pulse} 1.4s ease-in-out infinite;
`;

export const SkeletonCard = styled.div`
  background: ${theme.bgElev};
  border: 1px solid ${theme.surface[100]};
  border-radius: ${theme.radius.md};
  padding: 1.25rem;
  box-shadow: ${theme.shadow.sm};
`;

const AvatarCircle = styled.div<{ $size: number }>`
  width: ${(p) => p.$size}px;
  height: ${(p) => p.$size}px;
  border-radius: ${theme.radius.full};
  background: ${theme.accent};
  color: #fff;
  display: grid;
  place-items: center;
  font-weight: 700;
  font-size: ${(p) => Math.max(10, Math.round(p.$size * 0.34))}px;
  flex-shrink: 0;
  overflow: hidden;

  img {
    width: 100%;
    height: 100%;
    object-fit: cover;
  }
`;

function initialsFrom(name: string): string {
  const parts = name.trim().split(/[\s._-]+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[1][0]).toUpperCase();
}

export function Avatar({
  name,
  src,
  size = 36,
}: {
  name: string;
  src?: string;
  size?: number;
}) {
  return (
    <AvatarCircle $size={size} aria-hidden={!src} title={name}>
      {src ? <img src={src} alt="" /> : initialsFrom(name)}
    </AvatarCircle>
  );
}

const Catcher = styled.div`
  position: fixed;
  inset: 0;
  z-index: 40;
`;

const MenuPanel = styled.div<{ $align: "left" | "right" }>`
  position: absolute;
  top: calc(100% + 0.375rem);
  ${(p) => (p.$align === "right" ? "right: 0;" : "left: 0;")}
  min-width: 12rem;
  background: ${theme.bgElev};
  border: 1px solid ${theme.line};
  border-radius: ${theme.radius.xl};
  box-shadow: ${theme.shadow.xl};
  z-index: 50;
  padding: 0.375rem 0;
  overflow: hidden;
  animation: ${scaleIn} ${theme.motion.fast} ease-out;
  transform-origin: top ${(p) => (p.$align === "right" ? "right" : "left")};
`;

export const MenuItem = styled.button`
  width: 100%;
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 0.625rem 1rem;
  border: 0;
  background: transparent;
  color: ${theme.surface[600]};
  font: inherit;
  font-size: 0.875rem;
  text-align: left;
  cursor: pointer;
  transition: background ${theme.motion.fast} ease-out;

  &:hover {
    background: ${theme.surface[50]};
  }

  &[data-danger="true"] {
    color: #dc2626;
  }

  &[data-danger="true"]:hover {
    background: #fef2f2;
  }

  svg {
    width: 1rem;
    height: 1rem;
    color: ${theme.surface[400]};
    flex-shrink: 0;
  }

  &[data-danger="true"] svg {
    color: currentColor;
  }
`;

export function Dropdown({
  open,
  onClose,
  align = "left",
  children,
}: {
  open: boolean;
  onClose: () => void;
  align?: "left" | "right";
  children: ReactNode;
}) {
  if (!open) return null;
  return (
    <>
      <Catcher onClick={onClose} />
      <MenuPanel $align={align} role="menu">
        {children}
      </MenuPanel>
    </>
  );
}

const Backdrop = styled.button<{ $heavy?: boolean }>`
  position: fixed;
  inset: 0;
  z-index: 60;
  border: 0;
  padding: 0;
  cursor: default;
  background: ${(p) => (p.$heavy ? "rgba(15, 23, 42, 0.45)" : "rgba(15, 23, 42, 0.40)")};
  backdrop-filter: blur(4px);
`;

const ModalWrap = styled.div`
  position: fixed;
  inset: 0;
  z-index: 61;
  display: grid;
  place-items: center;
  padding: 1rem;
`;

const ModalPanel = styled.div<{ $elevated?: boolean }>`
  width: min(${(p) => (p.$elevated ? "28rem" : "32rem")}, 100%);
  background: ${theme.bgElev};
  border: 1px solid ${theme.line};
  border-radius: ${(p) => (p.$elevated ? theme.radius["2xl"] : theme.radius.xl)};
  box-shadow: ${(p) => (p.$elevated ? theme.shadow["2xl"] : theme.shadow.lg)};
  overflow: hidden;
  animation: ${scaleIn} ${theme.motion.base} ease-out;
`;

const AccentBar = styled.div<{ $tone: "danger" | "warn" | "primary" }>`
  height: 3px;
  background: ${(p) =>
    p.$tone === "danger"
      ? "linear-gradient(90deg, #ef4444, #e11d48)"
      : p.$tone === "warn"
        ? "linear-gradient(90deg, #fbbf24, #f97316)"
        : `linear-gradient(90deg, ${theme.accent}, ${theme.primary[700]})`};
`;

export function Modal({
  open,
  onClose,
  title,
  children,
  footer,
}: {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <>
      <Backdrop type="button" aria-label="Close dialog" onClick={onClose} />
      <ModalWrap role="dialog" aria-modal="true">
        <ModalPanel
          onClick={(e) => e.stopPropagation()}
          onKeyDown={(e) => e.stopPropagation()}
        >
          {title ? (
            <div
              style={{
                padding: "1rem 1.25rem",
                borderBottom: `1px solid ${theme.surface[100]}`,
                fontWeight: 600,
                fontSize: "0.95rem",
              }}
            >
              {title}
            </div>
          ) : null}
          <div style={{ padding: "1.25rem" }}>{children}</div>
          {footer ? (
            <div
              style={{
                padding: "0.85rem 1.25rem 1.15rem",
                display: "flex",
                justifyContent: "flex-end",
                gap: "0.5rem",
              }}
            >
              {footer}
            </div>
          ) : null}
        </ModalPanel>
      </ModalWrap>
    </>
  );
}

const ConfirmBody = styled.div`
  padding: 1.5rem;
  display: flex;
  align-items: flex-start;
  gap: 1rem;
`;

const ConfirmIcon = styled.div<{ $tone: "danger" | "warn" }>`
  width: 2.75rem;
  height: 2.75rem;
  border-radius: ${theme.radius.full};
  display: grid;
  place-items: center;
  flex-shrink: 0;
  background: ${(p) => (p.$tone === "danger" ? "#fef2f2" : "#fffbeb")};
  border: 1px solid ${(p) => (p.$tone === "danger" ? "#fee2e2" : "#fde68a")};
  color: ${(p) => (p.$tone === "danger" ? theme.critical : theme.high)};
`;

const WarnStrip = styled.div`
  margin: 0 1.5rem 1.25rem;
  padding: 0.65rem 0.85rem;
  border-radius: ${theme.radius.md};
  background: #fef2f2;
  border: 1px solid #fecaca;
  color: #991b1b;
  font-size: 0.8rem;
  line-height: 1.4;
`;

export function ConfirmModal({
  open,
  title,
  message,
  detail,
  confirmLabel = "Confirm",
  variant = "danger",
  loading = false,
  countdownSeconds = 0,
  onCancel,
  onConfirm,
}: {
  open: boolean;
  title: string;
  message: ReactNode;
  detail?: ReactNode;
  confirmLabel?: string;
  variant?: "danger" | "warn";
  loading?: boolean;
  countdownSeconds?: number;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const [remaining, setRemaining] = useState(0);

  useEffect(() => {
    if (!open) {
      setRemaining(0);
      return;
    }
    setRemaining(Math.max(0, Math.floor(countdownSeconds)));
  }, [open, countdownSeconds]);

  useEffect(() => {
    if (!open || remaining <= 0) return;
    const id = window.setTimeout(() => setRemaining((n) => Math.max(0, n - 1)), 1000);
    return () => window.clearTimeout(id);
  }, [open, remaining]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !loading) onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, loading, onCancel]);

  if (!open) return null;
  const gated = remaining > 0;
  return (
    <>
      <Backdrop
        type="button"
        $heavy
        aria-label="Close confirmation dialog"
        onClick={() => {
          if (!loading) onCancel();
        }}
      />
      <ModalWrap role="alertdialog" aria-modal="true" aria-labelledby="confirm-title">
        <ModalPanel $elevated>
          <AccentBar $tone={variant} />
          <ConfirmBody>
            <ConfirmIcon $tone={variant}>
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <path
                  d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </ConfirmIcon>
            <div>
              <div
                id="confirm-title"
                style={{ fontWeight: 700, fontSize: "1rem", color: theme.surface[800] }}
              >
                {title}
              </div>
              <p style={{ margin: "0.35rem 0 0", fontSize: "0.875rem", color: theme.muted }}>
                {message}
              </p>
            </div>
          </ConfirmBody>
          {variant === "danger" && detail ? <WarnStrip>{detail}</WarnStrip> : null}
          <div
            style={{
              padding: "0 1.5rem 1.25rem",
              display: "flex",
              justifyContent: "flex-end",
              gap: "0.5rem",
            }}
          >
            <Button type="button" variant="secondary" disabled={loading} onClick={onCancel}>
              Cancel
            </Button>
            <Button
              type="button"
              variant={variant === "danger" ? "destructive" : "primary"}
              loading={loading}
              disabled={gated || loading}
              onClick={onConfirm}
            >
              {gated ? `Wait ${remaining}s` : confirmLabel}
            </Button>
          </div>
        </ModalPanel>
      </ModalWrap>
    </>
  );
}

export const TableWrap = styled.div`
  overflow: auto;
  background: ${theme.bgElev};
  border: 1px solid ${theme.surface[100]};
  border-radius: ${theme.radius.md};
  box-shadow: ${theme.shadow.sm};
  margin: 0 0 1.5rem;
`;


export const DataTable = styled.table`
  width: 100%;
  border-collapse: collapse;
  font-size: 0.875rem;

  thead {
    position: sticky;
    top: 0;
    background: ${theme.surface[50]};
    z-index: 1;
  }

  th {
    text-align: left;
    padding: 0.625rem 1.25rem;
    color: ${theme.surface[500]};
    font-weight: 600;
    font-size: 11px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    border-bottom: 1px solid ${theme.surface[100]};
  }

  td {
    text-align: left;
    padding: 0.7rem 1.25rem;
    color: ${theme.surface[700]};
    border-bottom: 1px solid ${theme.surface[100]};
    vertical-align: middle;
  }

  tbody tr:last-child td {
    border-bottom: 0;
  }

  tbody tr {
    transition: background ${theme.motion.fast} ease-out;
  }

  tbody tr:hover {
    background: ${theme.surface[50]};
  }
`;

export function MailIcon() {
  return (
    <svg width="32" height="32" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M21.75 6.75v10.5a2.25 2.25 0 01-2.25 2.25h-15a2.25 2.25 0 01-2.25-2.25V6.75m19.5 0A2.25 2.25 0 0019.5 4.5h-15a2.25 2.25 0 00-2.25 2.25m19.5 0v.243a2.25 2.25 0 01-1.07 1.916l-7.5 4.615a2.25 2.25 0 01-2.36 0L3.32 8.91a2.25 2.25 0 01-1.07-1.916V6.75"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function ClusterIcon() {
  return (
    <svg width="32" height="32" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M6.429 9.75L2.25 12l4.179 2.25m0-4.5l5.571 3 5.571-3m-11.142 0L2.25 7.5 12 2.25l9.75 5.25-4.179 2.25m0 0L21.75 12l-4.179 2.25m0 0l4.179 2.25L12 21.75 2.25 16.5l4.179-2.25m11.142 0l-5.571 3-5.571-3"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function UsersIcon() {
  return (
    <svg width="32" height="32" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M15 19.128A9.38 9.38 0 0021.75 12c0-5.385-4.365-9.75-9.75-9.75S2.25 6.615 2.25 12a9.38 9.38 0 006.75 7.128M15 19.128v-.003c0-1.113-.285-2.16-.786-3.07M15 19.128v.106A12.318 12.318 0 0112 21.75c-2.305 0-4.47-.646-6.307-1.776l-.013-.01M9 11.25a3 3 0 106 0 3 3 0 00-6 0z"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function AuditIcon() {
  return (
    <svg width="32" height="32" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function WizardIcon() {
  return (
    <svg width="32" height="32" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.091zM18.259 8.715L18 9.75l-.259-1.035a3.375 3.375 0 00-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 002.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 002.456 2.456L21.75 6l-1.035.259a3.375 3.375 0 00-2.456 2.456z"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function SettingsIcon() {
  return (
    <svg width="32" height="32" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M9.594 3.94c.09-.542.56-.94 1.11-.94h2.593c.55 0 1.02.398 1.11.94l.213 1.281c.063.374.313.686.645.87.074.04.147.082.22.127.324.196.72.257 1.075.124l1.217-.456a1.125 1.125 0 011.37.49l1.296 2.247a1.125 1.125 0 01-.26 1.431l-1.003.827c-.293.24-.438.613-.43.992a6.759 6.759 0 010 .255c-.008.378.137.75.43.99l1.004.827c.424.35.534.955.26 1.43l-1.298 2.247a1.125 1.125 0 01-1.369.491l-1.217-.456c-.355-.133-.75-.072-1.076.124a6.57 6.57 0 01-.22.128c-.331.183-.581.495-.644.869l-.213 1.28c-.09.543-.56.941-1.11.941h-2.594c-.55 0-1.02-.398-1.11-.94l-.213-1.281c-.062-.374-.312-.686-.644-.87a6.52 6.52 0 01-.22-.127c-.325-.196-.72-.257-1.076-.124l-1.217.456a1.125 1.125 0 01-1.369-.49l-1.297-2.247a1.125 1.125 0 01.26-1.431l1.004-.827c.292-.24.437-.613.43-.992a6.932 6.932 0 010-.255c.007-.378-.138-.75-.43-.99l-1.004-.828a1.125 1.125 0 01-.26-1.43l1.297-2.247a1.125 1.125 0 011.37-.491l1.216.456c.356.133.751.072 1.076-.124.072-.044.146-.087.22-.128.332-.183.582-.495.644-.869l.214-1.281z"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
