import styled from "@emotion/styled";
import { Button } from "../ui";
import { theme } from "../styles/theme";

export type ObservabilitySnap = {
  present?: boolean;
  status?: "absent" | "unreachable" | "healthy" | string;
  ip?: string;
  hostname?: string;
  reachable?: boolean;
  can_add?: boolean;
  can_remove?: boolean;
};

type MailNode = {
  name: string;
  healthy: boolean;
  ip?: string;
};

const Wrap = styled.section`
  border: 1px solid ${theme.line};
  background: ${theme.bgElev};
  border-radius: ${theme.radius.md};
  padding: 1rem 1.1rem 1.05rem;
  box-shadow: ${theme.shadow.sm};
  margin: 0 0 1.15rem;
`;

const Head = styled.div`
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 0.75rem;
  margin: 0 0 0.65rem;
`;

const Title = styled.h2`
  margin: 0;
  font-size: 0.8rem;
  font-weight: 650;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: ${theme.surface[500]};
`;

const Caption = styled.p`
  margin: 0;
  font-size: 0.75rem;
  color: ${theme.muted};
`;

const SvgWrap = styled.div`
  width: 100%;
  overflow: hidden;
`;

const Actions = styled.div`
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 0.5rem;
  margin-top: 0.35rem;

  @media (max-width: 520px) {
    flex-direction: column;
    align-items: stretch;

    button {
      width: 100%;
    }
  }
`;

const OK = theme.ok;
const DOWN = theme.danger;
const MUTED = theme.surface[300];
const INK = theme.surface[800];
const SUB = theme.surface[500];

function cable(healthy: boolean, present: boolean): string {
  if (!present) return MUTED;
  return healthy ? OK : DOWN;
}

function NodeGlyph({
  x,
  y,
  title,
  subtitle,
  ip,
  healthy,
  placeholder,
}: {
  x: number;
  y: number;
  title: string;
  subtitle: string;
  ip?: string;
  healthy: boolean;
  placeholder?: boolean;
}) {
  const stroke = placeholder ? MUTED : healthy ? OK : DOWN;
  const fill = placeholder ? theme.surface[50] : "#fff";
  const addr = (ip || "").trim();
  return (
    <g>
      <rect
        x={x - 86}
        y={y - 44}
        width={172}
        height={addr ? 92 : 76}
        rx={14}
        fill={fill}
        stroke={stroke}
        strokeWidth={placeholder ? 1.25 : 1.6}
        strokeDasharray={placeholder ? "5 4" : undefined}
        filter={placeholder ? undefined : "url(#topoShadow)"}
      />
      <circle cx={x - 62} cy={y} r={7} fill={placeholder ? MUTED : stroke} />
      <text
        x={x - 46}
        y={y - 12}
        fill={SUB}
        fontSize={10}
        fontWeight={600}
        letterSpacing="0.04em"
      >
        {subtitle.toUpperCase()}
      </text>
      <text x={x - 46} y={y + 8} fill={INK} fontSize={13} fontWeight={650}>
        {title.length > 22 ? `${title.slice(0, 20)}…` : title}
      </text>
      {addr ? (
        <text x={x - 46} y={y + 26} fill={SUB} fontSize={11}>
          {addr}
        </text>
      ) : null}
    </g>
  );
}

export function ClusterTopology({
  topology = "2vm",
  mailNodes,
  observability,
  ops,
  busy,
  onAdd,
  onRemove,
}: {
  topology?: "1vm" | "2vm";
  mailNodes: MailNode[];
  observability: ObservabilitySnap;
  ops: boolean;
  busy?: boolean;
  onAdd: () => void;
  onRemove: () => void;
}) {
  if (topology === "1vm") {
    const node = mailNodes[0] || { name: "This server", healthy: true };
    return (
      <Wrap aria-label="Cluster topology">
        <Head>
          <Title>Topology</Title>
          <Caption>Single mail server. Add a second server when you are ready for HA.</Caption>
        </Head>
        <SvgWrap>
          <svg viewBox="0 0 640 160" width="100%" height="auto" role="img">
            <title>Single-server topology</title>
            <defs>
              <filter id="topoShadow" x="-20%" y="-20%" width="140%" height="140%">
                <feDropShadow dx="0" dy="2" stdDeviation="3" floodColor="rgba(15,23,42,0.12)" />
              </filter>
            </defs>
            <NodeGlyph
              x={320}
              y={80}
              title={node.name}
              subtitle="Mail"
              ip={node.ip}
              healthy={node.healthy}
            />
          </svg>
        </SvgWrap>
        {ops ? (
          <Actions>
            <Button type="button" disabled={busy} onClick={onAdd}>
              Add a second server
            </Button>
          </Actions>
        ) : null}
      </Wrap>
    );
  }

  const left = mailNodes[0] || { name: "Mail A", healthy: false };
  const right = mailNodes[1] || { name: "Mail B", healthy: false };
  const hasLeft = Boolean(mailNodes[0]);
  const hasRight = Boolean(mailNodes[1]);
  const obsStatus = observability.status || "absent";
  const obsPresent = obsStatus !== "absent";
  const obsHealthy = obsStatus === "healthy";
  const obsLabel =
    observability.hostname || observability.ip || (obsPresent ? "Observability" : "No Observability");
  const canAdd = ops && (observability.can_add ?? obsStatus === "absent");
  const canRemove = ops && (observability.can_remove ?? obsStatus === "unreachable");

  const mailLink = cable(hasLeft && hasRight && left.healthy && right.healthy, hasLeft && hasRight);
  const leftObs = cable(hasLeft && left.healthy && obsHealthy, obsPresent);
  const rightObs = cable(hasRight && right.healthy && obsHealthy, obsPresent);

  return (
    <Wrap aria-label="Cluster topology">
      <Head>
        <Title>Topology</Title>
        <Caption>DRBD between mail nodes. qdevice and SBD through Observability.</Caption>
      </Head>
      <SvgWrap>
        <svg viewBox="0 0 640 300" width="100%" height="auto" role="img">
          <title>Cluster topology</title>
          <defs>
            <filter id="topoShadow" x="-20%" y="-20%" width="140%" height="140%">
              <feDropShadow dx="0" dy="2" stdDeviation="3" floodColor="rgba(15,23,42,0.12)" />
            </filter>
          </defs>
          <line
            x1={140}
            y1={232}
            x2={500}
            y2={232}
            stroke={mailLink}
            strokeWidth={1.5}
            strokeLinecap="round"
          />
          <line
            x1={140}
            y1={232}
            x2={320}
            y2={58}
            stroke={leftObs}
            strokeWidth={1.5}
            strokeLinecap="round"
            strokeDasharray={obsPresent ? undefined : "5 4"}
          />
          <line
            x1={500}
            y1={232}
            x2={320}
            y2={58}
            stroke={rightObs}
            strokeWidth={1.5}
            strokeLinecap="round"
            strokeDasharray={obsPresent ? undefined : "5 4"}
          />
          <text x={320} y={222} textAnchor="middle" fill={SUB} fontSize={9} fontWeight={600}>
            DRBD
          </text>
          <text x={228} y={118} textAnchor="middle" fill={SUB} fontSize={9} fontWeight={600}>
            qdevice / SBD
          </text>
          <NodeGlyph
            x={320}
            y={58}
            title={obsLabel}
            subtitle="Observability"
            ip={observability.ip}
            healthy={obsHealthy}
            placeholder={!obsPresent}
          />
          <NodeGlyph
            x={140}
            y={232}
            title={left.name}
            subtitle="Mail"
            ip={left.ip}
            healthy={left.healthy}
            placeholder={!hasLeft}
          />
          <NodeGlyph
            x={500}
            y={232}
            title={right.name}
            subtitle="Mail"
            ip={right.ip}
            healthy={right.healthy}
            placeholder={!hasRight}
          />
        </svg>
      </SvgWrap>
      {ops ? (
        <Actions>
          <Button type="button" disabled={busy || !canAdd} onClick={onAdd}>
            Add Observability
          </Button>
          <Button type="button" variant="danger" disabled={busy || !canRemove} onClick={onRemove}>
            Remove Observability
          </Button>
        </Actions>
      ) : (
        <Caption style={{ textAlign: "center", marginTop: "0.4rem" }}>
          Observability add and remove require KIN Super Admin or Support-Ops.
        </Caption>
      )}
    </Wrap>
  );
}
