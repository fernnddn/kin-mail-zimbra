import styled from "@emotion/styled";
import { Button } from "../ui";
import { theme } from "../styles/theme";
import { charsThatFit, fitHostname, truncate } from "../lib/text";

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
  /** Promoted node serves mail; the other is the replica. */
  promoted?: boolean;
  /** Floating mail IP currently answering on this node. */
  vip?: string;
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

// Fixed card geometry. Everything else (connector endpoints, label chips) is
// derived from these, so a card can never grow into a line again.
const CARD_W = 200;
const CARD_H = 104;
const OBS_H = 84;

function NodeGlyph({
  cx,
  cy,
  height,
  title,
  subtitle,
  ip,
  healthy,
  placeholder,
  role,
  vip,
}: {
  cx: number;
  cy: number;
  height: number;
  title: string;
  subtitle: string;
  ip?: string;
  healthy: boolean;
  placeholder?: boolean;
  role?: string;
  vip?: string;
}) {
  const stroke = placeholder ? MUTED : healthy ? OK : DOWN;
  const fill = placeholder ? theme.bgPanel : theme.bgElev;
  const addr = (ip || "").trim();
  const floating = (vip || "").trim();
  const left = cx - CARD_W / 2;
  const top = cy - height / 2;
  // One consistent left margin for every line of text in the card.
  const textX = left + 20;
  // The badge's background rect is capped at the card width, but the text
  // inside it was not, so a longer role or an IPv6 VIP printed past the
  // rounded edge and off the card. 9px bold averages about 5.6px a character;
  // the pill costs 20px of padding on top of the card's own 20px inset.
  const badgeBudget = charsThatFit(CARD_W - 2 * 20 - 20, 5.6);
  const badgeFull = floating ? `${role} - VIP ${floating}` : role || "";
  // When the pair does not fit, drop the role WORD and keep the address.
  // "SERVING MAIL - VIP 198..." cut the one thing on the badge that cannot be
  // inferred from anywhere else on the card - the card is already labelled
  // Mail, and the promoted node is already drawn as the promoted one.
  const badge = truncate(
    badgeFull.length > badgeBudget && floating ? `VIP ${floating}` : badgeFull,
    badgeBudget,
  );

  return (
    <g>
      <rect
        x={left}
        y={top}
        width={CARD_W}
        height={height}
        rx={3}
        fill={fill}
        stroke={stroke}
        strokeWidth={placeholder ? 1.25 : 1.6}
        strokeDasharray={placeholder ? "5 4" : undefined}
        filter={placeholder ? undefined : "url(#topoShadow)"}
      />
      <circle cx={left + 12} cy={top + 17} r={4.5} fill={placeholder ? MUTED : stroke} />
      <text x={textX} y={top + 21} fill={SUB} fontSize={9} fontWeight={700} letterSpacing="0.06em">
        {subtitle.toUpperCase()}
      </text>
      <text x={textX} y={top + 42} fill={INK} fontSize={13} fontWeight={650}>
        {/* Budgeted against the card it has to sit in, rather than a magic 24
            that did not correspond to any width. 13px semibold averages about
            7.1px a character across the card's 20px inset on each side. */}
        {fitHostname(title, charsThatFit(CARD_W - 2 * 20, 7.1))}
        <title>{title}</title>
      </text>
      {addr ? (
        <text x={textX} y={top + 60} fill={SUB} fontSize={11}>
          {addr}
        </text>
      ) : null}
      {badge ? (
        <g>
          <rect
            x={textX - 6}
            y={top + height - 28}
            width={Math.min(CARD_W - 2 * (textX - left) + 12, badge.length * 5.6 + 14)}
            height={17}
            rx={2}
            fill={floating ? theme.accentSoft : theme.surface[100]}
          />
          <text
            x={textX}
            y={top + height - 16}
            fill={floating ? theme.accent : SUB}
            fontSize={9}
            fontWeight={700}
            letterSpacing="0.03em"
          >
            {badge}
          </text>
        </g>
      ) : null}
    </g>
  );
}

/** A label that sits ON its connector, with a chip so the line never crosses it. */
function LineLabel({ x, y, text }: { x: number; y: number; text: string }) {
  const w = text.length * 5.4 + 16;
  return (
    <g>
      <rect x={x - w / 2} y={y - 9} width={w} height={18} rx={2} fill={theme.paper} />
      <text
        x={x}
        y={y + 3.5}
        textAnchor="middle"
        fill={SUB}
        fontSize={9}
        fontWeight={700}
        letterSpacing="0.04em"
      >
        {text}
      </text>
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
  drbdUpToDate,
  drbdSyncPercent,
}: {
  topology?: "1vm" | "2vm";
  mailNodes: MailNode[];
  observability: ObservabilitySnap;
  ops: boolean;
  busy?: boolean;
  onAdd: () => void;
  onRemove: () => void;
  drbdUpToDate?: boolean;
  drbdSyncPercent?: number | null;
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
          <svg viewBox="0 0 760 172" width="100%" role="img">
            <title>Single-server topology</title>
            <defs>
              <filter id="topoShadow" x="-20%" y="-20%" width="140%" height="140%">
                <feDropShadow dx="0" dy="2" stdDeviation="3" floodColor="rgba(26, 25, 22, 0.10)" />
              </filter>
            </defs>
            <NodeGlyph
              cx={380}
              cy={86}
              height={OBS_H}
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

  // The link between the mail nodes IS the DRBD replication, so say what it
  // is doing rather than just naming the protocol.
  const drbdLabel = drbdUpToDate
    ? "DRBD IN SYNC"
    : typeof drbdSyncPercent === "number"
      ? `DRBD SYNCING ${drbdSyncPercent.toFixed(0)}%`
      : "DRBD";
  const drbdHealthy = Boolean(drbdUpToDate);
  const mailLink = cable(
    hasLeft && hasRight && left.healthy && right.healthy && drbdHealthy,
    hasLeft && hasRight,
  );
  const leftObs = cable(hasLeft && left.healthy && obsHealthy, obsPresent);
  const rightObs = cable(hasRight && right.healthy && obsHealthy, obsPresent);

  return (
    <Wrap aria-label="Cluster topology">
      <Head>
        <Title>Topology</Title>
        <Caption>DRBD between mail nodes. qdevice and SBD through Observability.</Caption>
      </Head>
      <SvgWrap>
        <svg viewBox="0 0 760 344" width="100%" role="img">
          <title>Cluster topology</title>
          <defs>
            <filter id="topoShadow" x="-20%" y="-20%" width="140%" height="140%">
              <feDropShadow dx="0" dy="2" stdDeviation="3" floodColor="rgba(26, 25, 22, 0.10)" />
            </filter>
          </defs>

          {/* Connectors run between card EDGES, never under a card: obs bottom
              centre down to each mail card's top centre, and a straight
              horizontal DRBD link between the two mail cards. */}
          <line
            x1={250}
            y1={250}
            x2={510}
            y2={250}
            stroke={mailLink}
            strokeWidth={1.75}
            strokeLinecap="round"
          />
          <line
            x1={380}
            y1={62 + OBS_H / 2}
            x2={150}
            y2={250 - CARD_H / 2}
            stroke={leftObs}
            strokeWidth={1.5}
            strokeLinecap="round"
            strokeDasharray={obsPresent ? undefined : "5 4"}
          />
          <line
            x1={380}
            y1={62 + OBS_H / 2}
            x2={610}
            y2={250 - CARD_H / 2}
            stroke={rightObs}
            strokeWidth={1.5}
            strokeLinecap="round"
            strokeDasharray={obsPresent ? undefined : "5 4"}
          />

          <LineLabel x={380} y={250} text={drbdLabel} />
          <LineLabel x={265} y={(62 + OBS_H / 2 + 250 - CARD_H / 2) / 2} text="QDEVICE + SBD" />
          <LineLabel x={495} y={(62 + OBS_H / 2 + 250 - CARD_H / 2) / 2} text="QDEVICE + SBD" />

          <NodeGlyph
            cx={380}
            cy={62}
            height={OBS_H}
            title={obsLabel}
            subtitle="Observability"
            ip={observability.ip}
            healthy={obsHealthy}
            placeholder={!obsPresent}
          />
          <NodeGlyph
            cx={150}
            cy={250}
            height={CARD_H}
            title={left.name}
            subtitle="Mail"
            ip={left.ip}
            healthy={left.healthy}
            placeholder={!hasLeft}
            role={hasLeft ? (left.promoted ? "SERVING MAIL" : "REPLICA") : undefined}
            vip={left.promoted ? left.vip : undefined}
          />
          <NodeGlyph
            cx={610}
            cy={250}
            height={CARD_H}
            title={right.name}
            subtitle="Mail"
            ip={right.ip}
            healthy={right.healthy}
            placeholder={!hasRight}
            role={hasRight ? (right.promoted ? "SERVING MAIL" : "REPLICA") : undefined}
            vip={right.promoted ? right.vip : undefined}
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
