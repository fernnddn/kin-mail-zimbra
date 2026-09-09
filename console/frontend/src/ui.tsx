import {
  useEffect,
  useId,
  useRef,
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
import { formatDeployLog } from "./wizard/ansi";
import kinMailLogo from "./assets/kinmail-lockup-black.png";

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
  background: ${theme.paper};
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

/* Gate screens: EULA, sign in, and the loading shells between them.
 *
 * A charcoal panel on the left carries the brand and one sentence saying what
 * this machine is; the paper side carries the form. The sentence lives there
 * so the form does not have to explain itself, which is how "Welcome Back /
 * Sign in to the KIN Mail admin console" turns into "Sign in".
 */
export const GateShell = styled.div`
  min-height: 100vh;
  display: grid;
  grid-template-columns: minmax(0, 5fr) minmax(0, 7fr);
  background: ${theme.paper};
  color: ${theme.ink};
  font-family: ${theme.font};

  @media (max-width: 860px) {
    /* Stacked, the charcoal band sizes to its own content instead of taking
       half the screen and leaving the form below the fold. */
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: auto minmax(0, 1fr);
  }
`;

export const GateAside = styled.aside`
  background: ${theme.rail};
  color: ${theme.paper};
  padding: 2.5rem 2.75rem;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  gap: 2rem;

  @media (max-width: 860px) {
    padding: 1.5rem 1.75rem;
    gap: 1rem;
  }
`;

export const GateAsideBody = styled.div`
  max-width: 34ch;

  p {
    margin: 1.5rem 0 0;
    font-size: 1.02rem;
    line-height: 1.55;
    color: rgba(244, 241, 234, 0.82);
  }

  @media (max-width: 860px) {
    max-width: none;

    p {
      margin-top: 0.9rem;
      font-size: 0.92rem;
    }
  }
`;

export const GateAsideFoot = styled.p`
  margin: 0;
  font-size: 0.74rem;
  line-height: 1.5;
  /* 0.45 measures 3.81 on the charcoal panel, and this is still text. */
  color: rgba(244, 241, 234, 0.6);

  @media (max-width: 860px) {
    display: none;
  }
`;

export const GateMain = styled.main`
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 2.5rem 2rem;
  min-width: 0;
`;

export const GateForm = styled.div`
  width: min(30rem, 100%);
`;

export const Card = styled.div`
  width: min(560px, 100%);
  background: ${theme.bgElev};
  border: 1px solid ${theme.line};
  border-radius: ${theme.radius.lg};
  padding: 1.6rem 1.5rem 1.4rem;
  box-shadow: none;
  animation: kin-fade-in ${theme.motion.base} ease-out;
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
  object-position: left center;
`;

/* The mark on a dark surface.
 *
 * It used to be `filter: invert(1) brightness(1.6)`, which is only safe on
 * artwork that is uniformly dark. This lockup is not: it is a black shield
 * holding a WHITE envelope and a RED dot. Inverting it produced a white blob
 * with a black envelope and a CYAN dot, which is the wrong brand colour on the
 * first screen anyone sees.
 *
 * So the artwork is never touched. It sits on a small paper plate instead,
 * which keeps the real shield, the real envelope and the real red dot on any
 * background and does not depend on how a browser chains filters. */
const LockupPlate = styled.span`
  display: inline-flex;
  align-items: center;
  background: ${theme.paper};
  border-radius: ${theme.radius.sm};
  padding: 0.3rem 0.55rem;
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
  light = false,
  alt = "KIN Mail",
  ...rest
}: { compact?: boolean; light?: boolean } & ImgHTMLAttributes<HTMLImageElement>) {
  const img = <LockupImg src={kinMailLogo} alt={alt} $compact={compact} {...rest} />;
  const inner = compact ? <CompactLockup>{img}</CompactLockup> : img;
  if (light) return <LockupPlate>{inner}</LockupPlate>;
  return inner;
}

export const Title = styled.h1`
  margin: 0 0 0.35rem;
  font-size: 1.55rem;
  font-weight: 700;
  letter-spacing: -0.015em;
  line-height: 1.15;
  color: ${theme.ink};
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

/* Ink, drawn just outside the border. The old 3px translucent glow was the
   last blue thing on the page and it bled over neighbouring fields on a dense
   form; 2px of solid ink is unmistakable without spreading. */
const focusRing = `
  &:focus {
    outline: none;
    border-color: ${theme.ink};
    box-shadow: 0 0 0 2px ${theme.ink};
  }
  &:focus-visible {
    outline: none;
    border-color: ${theme.ink};
    box-shadow: 0 0 0 2px ${theme.ink};
  }
`;

/* The same idea on charcoal. An ink ring on the rail measures about 1.3 against
   the rail itself, so a keyboard user would see nothing at all; on a dark
   surface the ring has to be paper. Exported because the rail lives in
   ConsoleChrome and the task buttons live in TaskCenter. */
export const focusRingOnDark = `
  &:focus {
    outline: none;
    box-shadow: inset 0 0 0 2px ${theme.paper};
  }
  &:focus-visible {
    outline: none;
    box-shadow: inset 0 0 0 2px ${theme.paper};
  }
`;

export const Input = styled.input`
  width: 100%;
  /* surface[300], not theme.line. The line token is the decorative hairline
     that separates sections; a field is a control the operator has to find.
     The restyle dropped this to that hairline and left a text input visibly
     fainter than the select sitting under it on the same form. */
  border: 1px solid ${theme.surface[300]};
  background: ${theme.bgElev};
  color: ${theme.ink};
  border-radius: ${theme.radius.sm};
  padding: 0.45rem 0.7rem;
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
  /* Show/hide is an icon-only control, so the glyph itself carries the
     meaning and owes 3:1. */
  color: ${theme.muted};
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
  border-radius: ${theme.radius.sm};
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
  border-radius: ${theme.radius.sm};
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

/* Oxide is the one loud colour on the page and it means "this commits
   something": sign in, deploy, create the mailbox, apply the licence, remove
   the host. Everything else is an ink outline. If two buttons on a screen are
   oxide, one of them is wrong. */
const isCommit = (v: ButtonVariant) =>
  v === "primary" || v === "danger" || v === "destructive";

const ButtonRoot = styled.button<{ $variant: ButtonVariant }>`
  border: ${(p) =>
    isCommit(p.$variant)
      ? `1px solid ${theme.oxide}`
      : p.$variant === "secondary"
        ? `1px solid ${theme.ink}`
        : "1px solid transparent"};
  border-radius: ${theme.radius.sm};
  background: ${(p) => (isCommit(p.$variant) ? theme.oxide : "transparent")};
  color: ${(p) => (isCommit(p.$variant) ? theme.paper : theme.ink)};
  font: inherit;
  font-size: 0.875rem;
  font-weight: 600;
  padding: 0.45rem 0.9rem;
  min-height: 2.25rem;
  cursor: pointer;
  box-shadow: none;
  transition:
    background ${theme.motion.fast} ease-out,
    border-color ${theme.motion.fast} ease-out,
    color ${theme.motion.fast} ease-out;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 0.45rem;

  &:hover:not(:disabled) {
    background: ${(p) =>
      isCommit(p.$variant) ? theme.oxideHover : theme.surface[100]};
    border-color: ${(p) => (isCommit(p.$variant) ? theme.oxideHover : theme.ink)};
  }

  /* No press-scale. A button that shrinks reads as a toy on a tool that
     removes cluster nodes. */
  &:active:not(:disabled) {
    background: ${(p) =>
      isCommit(p.$variant) ? theme.oxideHover : theme.surface[200]};
  }

  &:disabled {
    opacity: 1;
    cursor: not-allowed;
    box-shadow: none;
    background: transparent;
    color: ${theme.surface[400]};
    border: 1px solid ${theme.surface[300]};
  }

  ${focusRing}
`;

/* Three marks that breathe in sequence, not a rotating donut.
   
   The rotating border-circle is the default every framework ships, and it
   looked borrowed here: this product is flat paper and ink, its corner radius
   is 2px because there are no pills on paper, and a spinning ring was the one
   piece of somebody else's design language in it. It also spins at a constant
   rate whatever is happening, which reads as "frozen" the moment a task takes
   longer than expected.
   
   A staggered fade says the same thing more quietly, sits still on the
   baseline next to text, and matches the squares used everywhere else. The
   $size prop is kept so every existing call site keeps working unchanged. */
const march = keyframes`
  0%, 80%, 100% { opacity: 0.2; transform: scaleY(0.7); }
  40% { opacity: 1; transform: scaleY(1); }
`;

export const Spinner = styled.span<{ $size?: number }>`
  display: inline-flex;
  align-items: center;
  gap: ${(p) => Math.max(2, (p.$size ?? 14) / 5)}px;
  height: ${(p) => (p.$size ?? 14) / 16}rem;
  flex-shrink: 0;
  vertical-align: middle;

  &::before,
  &::after {
    content: "";
    width: ${(p) => Math.max(2, (p.$size ?? 14) / 4.5)}px;
    height: ${(p) => (p.$size ?? 14) / 16}rem;
    background: currentColor;
    border-radius: 1px;
    animation: ${march} 1.1s ${theme.motion.ease.standard} infinite;
  }
  &::before {
    animation-delay: -0.22s;
  }
  &::after {
    animation-delay: 0.22s;
  }
`;

/* An indeterminate bar, for the places with room for something calmer than an
   inline indicator: a long install where the operator wants to know the box is
   still working without being made to stare at a spinning ring. */
const sweep = keyframes`
  0% { transform: translateX(-100%) scaleX(0.4); }
  50% { transform: translateX(0%) scaleX(0.7); }
  100% { transform: translateX(100%) scaleX(0.4); }
`;

export const ProgressTrack = styled.div`
  position: relative;
  height: 3px;
  width: 100%;
  overflow: hidden;
  background: color-mix(in srgb, currentColor 14%, transparent);
  border-radius: 2px;
`;

export const ProgressSweep = styled.span`
  position: absolute;
  inset: 0;
  background: currentColor;
  border-radius: 2px;
  transform-origin: center;
  animation: ${sweep} 1.5s ${theme.motion.ease.standard} infinite;
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
  color: ${theme.ink};
  font-size: 0.875rem;
  margin: 0 0 0.85rem;
  padding: 0.625rem 0.95rem;
  background: ${theme.oxideSoft};
  border: 1px solid ${theme.line};
  border-left: 3px solid ${theme.oxide};
  border-radius: ${theme.radius.sm};
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

/* The chosen option carries a 3px ink edge and its title goes bold. A 1px ink
   border against a 1px rule border was almost invisible, and on the topology
   step that choice decides the whole deployment. */
export const Choice = styled.button<{ selected?: boolean }>`
  text-align: left;
  border: 1px solid ${(p) => (p.selected ? theme.ink : theme.surface[300])};
  border-left: ${(p) => (p.selected ? `3px solid ${theme.ink}` : `1px solid ${theme.surface[300]}`)};
  background: ${(p) => (p.selected ? theme.accentSoft : theme.bgElev)};
  color: ${theme.ink};
  border-radius: ${theme.radius.sm};
  padding: 0.9rem 1rem;
  padding-left: ${(p) => (p.selected ? "calc(1rem - 2px)" : "1rem")};
  cursor: pointer;
  font: inherit;
  box-shadow: none;
  transition:
    border-color ${theme.motion.fast} ease-out,
    background ${theme.motion.fast} ease-out;

  &:hover {
    border-color: ${theme.ink};
  }

  ${focusRing}

  strong {
    display: block;
    margin-bottom: 0.25rem;
    font-weight: ${(p) => (p.selected ? 700 : 600)};
  }

  /* Selecting a card washes it with 6 percent ink, which drags mute text on
     it down to 4.43 against that wash. The description of the option you have
     actually chosen has to stay readable, so it steps one stop darker. */
  span {
    color: ${(p) => (p.selected ? theme.surface[600] : theme.muted)};
    font-size: 0.86rem;
    line-height: 1.4;
  }
`;

/* Louder than WarnBox, and deliberately rare. WarnBox means "look at this
   when you can". This one is for a state that is about to cost service or
   data if the operator does nothing in the next minute, which on this
   appliance means exactly one thing: a node that is going to reset itself. */
export const DangerBox = styled.div`
  border: 1px solid ${theme.oxide};
  border-left: 3px solid ${theme.oxide};
  background: ${theme.oxideSoft};
  color: ${theme.ink};
  border-radius: ${theme.radius.sm};
  padding: 0.8rem 1rem;
  margin-bottom: 1rem;
  font-size: 0.9rem;
  line-height: 1.55;

  strong {
    color: ${theme.oxide};
  }
`;

export const WarnBox = styled.div`
  border: 1px solid ${theme.line};
  border-left: 3px solid ${theme.warn};
  background: ${theme.warnSoft};
  color: ${theme.ink};
  border-radius: ${theme.radius.sm};
  padding: 0.75rem 0.95rem;
  margin-bottom: 1rem;
  font-size: 0.88rem;
  line-height: 1.5;

  /* Ink, for the same reason the warn pill is ink: ochre over its own wash
     does not clear AA, and the 3px ochre edge already marks this as a
     warning. */
  strong {
    color: ${theme.ink};
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
  /* Recessed paper, not a black terminal. The log is part of the page, and
     an operator reads it for minutes at a time. */
  background: ${theme.bgPanel};
  border: 1px solid ${theme.line};
  border-radius: ${theme.radius.sm};
  padding: 0.9rem 1rem;
  color: ${theme.ink};
  font-family: ${theme.mono};
  font-size: 0.8rem;
  line-height: 1.5;
  white-space: pre-wrap;
  box-shadow: none;
`;

/**
 * A log pane that shows what the operator meant to read.
 *
 * The install scripts colour their output, and that colouring was being
 * rendered literally: "[36m=> [0m [1mTLS method: manual [0m" for one line of
 * an ordinary certificate renewal. Every log surface in the console had it,
 * which is most of what "the display is messy" meant.
 *
 * Stripping happens here, at the point of display, so what is written to disk
 * and streamed over SSE stays byte-for-byte what the script produced.
 */
export function LogView({
  children,
  ...rest
}: { children?: string } & ComponentProps<typeof LogPane>) {
  return <LogPane {...rest}>{formatDeployLog(children || "")}</LogPane>;
}

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

/** $wide: data-dense pages (Cluster, Monitoring) use the whole screen.
 *
 * 960px is right for a form but wastes most of a 1080p display on a dashboard,
 * which is why the Cluster page needed scrolling for content that had room to
 * sit side by side. Forms keep the narrow measure - a login box stretched to
 * 1900px is not an improvement.
 */
export const Page = styled.div<{ $wide?: boolean }>`
  padding: 1.6rem 1.75rem 2.5rem;
  max-width: ${(p) => (p.$wide ? "1680px" : "1080px")};
  width: 100%;
  margin: 0 auto;
  animation: kin-page-in ${theme.motion.page} ease-out;
`;

/* The title sits on a heavy ink baseline. That rule is the page's masthead:
   it says where you are without a coloured tile, a gradient or a shadow. */
const HeaderRow = styled.div`
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 1.25rem;
  flex-wrap: wrap;
  padding-bottom: 0.7rem;
  margin-bottom: 1.35rem;
  border-bottom: 2px solid ${theme.ink};
`;

const HeaderActions = styled.div`
  display: flex;
  align-items: center;
  gap: 0.5rem;
  flex-shrink: 0;
`;

const HeaderText = styled.div`
  min-width: 0;
  flex: 1 1 20rem;

  h1 {
    margin: 0;
    font-size: 1.55rem;
    font-weight: 700;
    letter-spacing: -0.015em;
    line-height: 1.15;
    color: ${theme.ink};
  }

  /* Quieter and narrower than the title on purpose: it is the sentence that
     explains the page, not a second heading. */
  p {
    margin: 0.3rem 0 0;
    font-size: 0.85rem;
    color: ${theme.muted};
    line-height: 1.5;
    max-width: 62ch;
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
  /* `icon` and `gradient` are accepted and deliberately not drawn. Every page
     used to open with a 3.5rem gradient tile and a coloured shadow, which is
     decoration on a tool whose pages are already named. The props stay so no
     call site has to change, and so the decision lives in one place rather
     than being re-litigated per page. */
  void icon;
  void gradient;
  return (
    <HeaderRow>
      <HeaderText>
        <h1>{title}</h1>
        {subtitle ? <p>{subtitle}</p> : null}
      </HeaderText>
      {action ? <HeaderActions>{action}</HeaderActions> : null}
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
  transition: background-color ${theme.motion.base} ${theme.motion.ease.standard};

  /* Pressing it should feel like pressing something. Without this the only
     feedback is the thumb arriving, which on a slow remote session can be
     late enough that the operator clicks twice. */
  &:active:not(:disabled) {
    transform: scale(0.96);
    transition: transform ${theme.motion.fast} ${theme.motion.ease.standard};
  }

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
  border-radius: ${theme.radius.sm};
  background: ${theme.paper};
  box-shadow: none;
  transform: translateX(${(p) => (p.$on ? "20px" : "0")});
  /* Emphasis easing: the thumb travels a visible distance, and settling into
     the end instead of stopping dead is what makes the control read as a
     physical switch rather than two states swapped. */
  transition: transform ${theme.motion.slow} ${theme.motion.ease.emphasis};
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
  critical: { fg: theme.oxide, bg: theme.oxideSoft, border: "rgba(180, 35, 24, 0.28)" },
  /* Ink on the ochre tint, not ochre on the ochre tint. Ochre text over its
   * own 12 percent wash measures 3.88 against paper, which fails AA, and these
   * pills are small text. The tint and the border still say "warning"; the
   * word inside just has to be readable. Every other tone clears AA on its own
   * tint and keeps its colour. */
  high: { fg: theme.ink, bg: theme.warnSoft, border: "rgba(148, 101, 12, 0.30)" },
  medium: { fg: theme.ink, bg: theme.warnSoft, border: "rgba(148, 101, 12, 0.30)" },
  low: { fg: theme.ok, bg: theme.okSoft, border: "rgba(47, 111, 78, 0.26)" },
  completed: { fg: theme.ok, bg: theme.okSoft, border: "rgba(47, 111, 78, 0.26)" },
  primary: { fg: theme.ink, bg: theme.accentSoft, border: theme.surface[300] },
  warn: { fg: theme.ink, bg: theme.warnSoft, border: "rgba(148, 101, 12, 0.30)" },
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
  border-radius: ${theme.radius.sm};
  background: ${theme.ink};
  color: ${theme.paper};
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

const MenuPanel = styled.div<{ $align: "left" | "right"; $drop: "down" | "up" }>`
  position: absolute;
  ${(p) => (p.$drop === "up" ? "bottom: calc(100% + 0.375rem);" : "top: calc(100% + 0.375rem);")}
  ${(p) => (p.$align === "right" ? "right: 0;" : "left: 0;")}
  min-width: 12rem;
  background: ${theme.bgElev};
  border: 1px solid ${theme.line};
  border-radius: ${theme.radius.lg};
  box-shadow: ${theme.shadow.lg};
  z-index: 50;
  padding: 0.25rem 0;
  overflow: hidden;
  animation: ${scaleIn} ${theme.motion.fast} ease-out;
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
    color: ${theme.oxide};
  }

  &[data-danger="true"]:hover {
    background: ${theme.oxideSoft};
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
  drop = "down",
  children,
}: {
  open: boolean;
  onClose: () => void;
  align?: "left" | "right";
  /** "up" for a trigger that sits at the bottom of the viewport, such as the
   * account menu in the foot of the ops rail. */
  drop?: "down" | "up";
  children: ReactNode;
}) {
  if (!open) return null;
  return (
    <>
      <Catcher onClick={onClose} />
      <MenuPanel $align={align} $drop={drop} role="menu">
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
  background: ${(p) => (p.$heavy ? "rgba(26, 25, 22, 0.55)" : "rgba(26, 25, 22, 0.42)")};
`;

const ModalWrap = styled.div`
  position: fixed;
  inset: 0;
  z-index: 61;
  display: grid;
  place-items: center;
  padding: 1rem;
`;

/* Both dialogs declare aria-modal="true", which tells assistive tech the rest
   of the page is inert. That is only true if focus actually goes into the
   dialog, stays there while it is open, and returns where it came from on
   close. Without this a keyboard user opening "Remove host" is left on the
   body, tabbing through a rail the dialog has just declared hidden. */
function useModalFocus(open: boolean) {
  const panelRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!open) return;
    const restoreTo = document.activeElement as HTMLElement | null;
    const panel = panelRef.current;
    const focusable = () =>
      Array.from(
        panel?.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), input:not([disabled]),' +
            ' select:not([disabled]), textarea:not([disabled]),' +
            ' [tabindex]:not([tabindex="-1"])',
        ) ?? [],
      );
    (focusable()[0] ?? panel)?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Tab" || !panel) return;
      const items = focusable();
      if (items.length === 0) {
        e.preventDefault();
        panel.focus();
        return;
      }
      const at = items.indexOf(document.activeElement as HTMLElement);
      if (e.shiftKey && at <= 0) {
        e.preventDefault();
        items[items.length - 1].focus();
      } else if (!e.shiftKey && (at === -1 || at === items.length - 1)) {
        e.preventDefault();
        items[0].focus();
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => {
      window.removeEventListener("keydown", onKey, true);
      restoreTo?.focus?.();
    };
  }, [open]);
  return panelRef;
}

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
    p.$tone === "danger" ? theme.oxide : p.$tone === "warn" ? theme.warn : theme.ink};
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
  const panelRef = useModalFocus(open);

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
          ref={panelRef}
          tabIndex={-1}
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
  background: ${(p) => (p.$tone === "danger" ? theme.oxideSoft : theme.warnSoft)};
  border: 1px solid ${(p) => (p.$tone === "danger" ? theme.oxide : theme.warn)};
  color: ${(p) => (p.$tone === "danger" ? theme.oxide : theme.warn)};
`;

const WarnStrip = styled.div`
  margin: 0 1.5rem 1.25rem;
  padding: 0.65rem 0.85rem;
  border-radius: ${theme.radius.sm};
  background: ${theme.oxideSoft};
  border: 1px solid ${theme.line};
  border-left: 3px solid ${theme.oxide};
  color: ${theme.ink};
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
  confirmDisabled = false,
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
  /** Extra gate for Confirm (e.g. probe still running) without blocking Cancel. */
  confirmDisabled?: boolean;
  countdownSeconds?: number;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const [remaining, setRemaining] = useState(0);
  const panelRef = useModalFocus(open);

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
        <ModalPanel ref={panelRef} tabIndex={-1} $elevated>
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
              disabled={gated || loading || confirmDisabled}
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
