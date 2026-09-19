import { useCallback, useState } from "react";
import styled from "@emotion/styled";
import { keyframes } from "@emotion/react";
import { theme } from "../styles/theme";
import { Button, ConfirmModal, Hint, LogPane, ProgressSweep, ProgressTrack } from "../ui";

/**
 * Extend a filesystem into space added to its disk by the hypervisor.
 *
 * The operator grows the virtual disk in VMware or Proxmox and the guest keeps
 * showing the old size, because a bigger disk is not a bigger partition and a
 * bigger partition is not a bigger filesystem. By hand that is three or four
 * tools deep depending on whether the appliance uses LVM and LUKS, every one of
 * them able to destroy the data if given the wrong argument.
 *
 * Always plan before applying. This is the most destructive button in the
 * product, so it does not offer to do anything until it has said what it would
 * do, and the operator has read it. The refusals all live in grow-disk.sh on
 * the appliance; this surface only shows what came back.
 */

type Phase = "idle" | "planning" | "planned" | "blocked" | "applying" | "done" | "failed";

const Wrap = styled.section`
  margin: 0 0 1.15rem;
`;

const Head = styled.div`
  display: flex;
  align-items: baseline;
  gap: 0.6rem;
  margin: 0 0 0.55rem;
  flex-wrap: wrap;
`;

const Name = styled.strong`
  font-size: 0.92rem;
`;

const Blurb = styled.span`
  color: ${theme.surface[500]};
  font-size: 0.82rem;
`;

/* Which machine the disks below are on. Only rendered when there is more than
   one, so a single appliance is not asked to read a heading answering a
   question it does not have. */
const GroupName = styled.h4`
  margin: 1rem 0 0.35rem;
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: ${theme.surface[500]};
`;

/* One self-contained block per disk: the row, and whatever the appliance said
   about it.

   Spacing used to come from `Row + Row`, which only works while the rows are
   actually adjacent. As soon as a result panel appeared under the first disk
   the two rows stopped being siblings, the margin vanished, and the panel sat
   on top of the row beneath it. Owning the spacing at the block level means it
   cannot depend on what is or is not showing inside. */
const TargetBlock = styled.div`
  & + & {
    margin-top: 0.6rem;
  }
`;

const Row = styled.div`
  display: flex;
  align-items: center;
  gap: 0.7rem;
  flex-wrap: wrap;
  padding: 0.75rem 0.85rem;
  border: 1px solid ${theme.line};
  border-radius: ${theme.radius.md};
  background: ${theme.bgElev};
`;

const RowName = styled.span`
  font-weight: 600;
  font-size: 0.88rem;
  min-width: 9.5rem;
`;

const Mount = styled.code`
  font-family: ${theme.mono};
  font-size: 0.78rem;
  color: ${theme.surface[500]};
`;

const Spacer = styled.span`
  flex: 1 1 auto;
`;

const panelEnter = keyframes`
  from { opacity: 0; transform: translateY(-4px); }
  to { opacity: 1; transform: none; }
`;

const Result = styled.div<{ $tone: "ok" | "warn" | "muted" }>`
  animation: ${panelEnter} ${theme.motion.slow} ${theme.motion.ease.standard} both;
  margin: 0.4rem 0 0;
  padding: 0.6rem 0.75rem;
  border: 1px solid ${theme.line};
  border-left: 3px solid
    ${(p) => (p.$tone === "ok" ? theme.ok : p.$tone === "warn" ? theme.warn : theme.line)};
  border-radius: ${theme.radius.md};
  background: ${theme.paper};
  font-size: 0.85rem;
  line-height: 1.5;
  /* Long refusals explain a partition layout in full sentences and have to
     wrap, not run under the next thing on the page. */
  white-space: pre-wrap;
  overflow-wrap: anywhere;
`;

const Working = styled.div`
  animation: ${panelEnter} ${theme.motion.slow} ${theme.motion.ease.standard} both;
  display: grid;
  gap: 0.45rem;
  margin: 0.4rem 0 0;
  padding: 0.6rem 0.75rem;
  border: 1px solid ${theme.line};
  border-radius: ${theme.radius.md};
  background: ${theme.paper};
  color: ${theme.surface[600]};
  font-size: 0.85rem;
`;

/** A disk the console can grow. `node` is the machine it is on: "" is the
    one the console runs on, "mailbox" is the other half of a multi
    deployment. The console sends a NAME for both, never a path or an
    address; the helper resolves them from the appliance config. */
type Target = {
  id: "system" | "mail";
  node: "" | "mailbox";
  key: string;
  name: string;
  mount: string;
  blurb: string;
};

const LOCAL_TARGETS: Target[] = [
  {
    id: "system",
    node: "",
    key: "this-system",
    name: "System disk",
    mount: "/",
    blurb: "The operating system, the console and the logs.",
  },
  {
    id: "mail",
    node: "",
    key: "this-mail",
    name: "Mail storage",
    mount: "/opt/zimbra",
    blurb: "Mailboxes, indexes and the mail queue.",
  },
];

/* The other machine of a multi deployment.
 *
 * Its disks are the ones that actually fill up - it holds every message - and
 * until now the console could not reach either of them. The operator never
 * logs into that machine by design, so "run it over there" was not an answer.
 *
 * Offered only when there IS one. A single appliance never sees this. */
const MAILBOX_TARGETS: Target[] = [
  {
    id: "system",
    node: "mailbox",
    key: "mailbox-system",
    name: "System disk",
    mount: "/",
    blurb: "The operating system and the logs on the mailbox.",
  },
  {
    id: "mail",
    node: "mailbox",
    key: "mailbox-mail",
    name: "Mail storage",
    mount: "/opt/zimbra",
    blurb: "Mailboxes, indexes and the mail queue. Every message is here.",
  },
];

/** Human summary from the helper's output, without the machine markers. */
function readable(log: string): string {
  return log
    .split("\n")
    .filter((line) => line.trim() && !line.startsWith("KIN_GROW_"))
    .join("\n")
    .trim();
}

function refusedReason(log: string): string {
  const found = log.match(/KIN_GROW_REFUSED reason=(\S+)/);
  return found ? found[1] : "";
}

function nothingToDo(log: string): boolean {
  return /KIN_GROW_NOTHING_TO_DO=1/.test(log);
}

function freeBytes(log: string): number {
  const found = log.match(/KIN_GROW_FREE_BYTES=(\d+)/);
  return found ? Number(found[1]) : 0;
}

/**
 * The mail disk is laid out as a big data partition followed by a small one
 * the installer reserves for a future second server. On a single-server
 * appliance that reserved partition is never used, and because it sits AFTER
 * the data partition it makes the mail disk permanently ungrowable: space
 * added in the hypervisor lands behind it and nothing can reach it.
 *
 * The helper says so by name when that is the only thing in the way.
 */
function reclaimableReserved(log: string): string {
  const found = log.match(/KIN_GROW_RECLAIMABLE_RESERVED=(\S+)/);
  return found ? found[1] : "";
}

function reservedButStuck(log: string): string {
  const found = log.match(/KIN_GROW_RESERVED_NOT_RECLAIMABLE=(\S+)/);
  return found ? found[1] : "";
}

function humanBytes(n: number): string {
  if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(1)} GB`;
  if (n >= 1024 ** 2) return `${Math.round(n / 1024 ** 2)} MB`;
  return `${n} bytes`;
}

function TargetRow({
  target,
  busy,
  onBusyChange,
  onGrew,
}: {
  target: Target;
  busy: boolean;
  onBusyChange: (busy: boolean) => void;
  onGrew: () => void;
}) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [log, setLog] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [reclaimOpen, setReclaimOpen] = useState(false);

  const stream = useCallback(
    (
      op: "plan" | "apply",
      onEnd: (code: number, text: string) => void,
      reclaim = false,
    ) => {
      let text = "";
      const es = new EventSource(
        `/api/wizard/deploy/stream?action=grow_disk&op=${op}&target=${target.id}` +
          (target.node ? `&node=${target.node}` : "") +
          (reclaim ? "&reclaim=1" : ""),
      );
      let code: number | null = null;
      es.onmessage = (ev) => {
        try {
          const data = JSON.parse(ev.data) as {
            type?: string;
            data?: string;
            exit_code?: number;
            message?: string;
          };
          if ((data.type === "stdout" || data.type === "stderr") && data.data) {
            text += data.data;
            setLog(text.slice(-8000));
          } else if (data.type === "error") {
            text += `[error] ${data.message || ""}\n`;
            setLog(text.slice(-8000));
          } else if (data.type === "done") {
            code = typeof data.exit_code === "number" ? data.exit_code : 1;
            es.close();
            onEnd(code, text);
          }
        } catch {
          /* ignore malformed SSE */
        }
      };
      es.onerror = () => {
        es.close();
        if (code === null) onEnd(1, text + "Lost connection to the appliance.\n");
      };
    },
    [target.id],
  );

  const plan = useCallback(() => {
    setPhase("planning");
    setLog("");
    onBusyChange(true);
    stream("plan", (code, text) => {
      onBusyChange(false);
      // A non-zero plan is not an error to apologise for: it is the helper
      // saying there is nothing to do, or refusing for a reason worth reading.
      if (code === 0) setPhase("planned");
      else setPhase(nothingToDo(text) || refusedReason(text) ? "blocked" : "failed");
    });
  }, [stream, onBusyChange]);

  const apply = useCallback(
    (reclaim = false) => {
      setConfirmOpen(false);
      setReclaimOpen(false);
      setPhase("applying");
      onBusyChange(true);
      stream(
        "apply",
        (code) => {
          onBusyChange(false);
          setPhase(code === 0 ? "done" : "failed");
          if (code === 0) onGrew();
        },
        reclaim,
      );
    },
    [stream, onBusyChange, onGrew],
  );

  const free = freeBytes(log);
  const summary = readable(log);
  const working = phase === "planning" || phase === "applying";
  const reserved = reclaimableReserved(log);
  const reservedStuck = reservedButStuck(log);

  return (
    <TargetBlock>
      <Row>
        <RowName>{target.name}</RowName>
        <Mount>{target.mount}</Mount>
        <Spacer />
        {phase === "planned" && free > 0 ? (
          <Button
            type="button"
            variant="primary"
            disabled={busy}
            onClick={() => setConfirmOpen(true)}
          >
            Extend by {humanBytes(free)}
          </Button>
        ) : reserved ? (
          /* The one case where "Check again" would just repeat itself for
             ever: the disk has room, and a partition nobody uses is in the
             way. Offer the thing that actually helps. */
          <Button
            type="button"
            variant="primary"
            disabled={busy}
            onClick={() => setReclaimOpen(true)}
          >
            Free the reserved partition and extend
          </Button>
        ) : (
          <Button
            type="button"
            variant="ghost"
            loading={phase === "planning"}
            disabled={busy}
            onClick={plan}
          >
            {phase === "idle" ? "Check for unused space" : "Check again"}
          </Button>
        )}
      </Row>

      {working ? (
        <Working>
          <ProgressTrack>
            <ProgressSweep />
          </ProgressTrack>
          <span>
            {phase === "planning"
              ? `Reading the disk behind ${target.mount}. Nothing is being changed.`
              : `Extending ${target.mount}. Mail keeps running and nothing is unmounted.`}
          </span>
        </Working>
      ) : null}

      {!working && summary ? (
        <Result
          $tone={phase === "done" ? "ok" : phase === "failed" ? "warn" : phase === "planned" ? "ok" : "muted"}
        >
          {summary}
        </Result>
      ) : null}

      {phase === "failed" && log ? <LogPane>{log}</LogPane> : null}

      <ConfirmModal
        open={confirmOpen}
        title={`Extend ${target.name.toLowerCase()}?`}
        message={`${target.mount} will grow by about ${humanBytes(free)} into the free space that follows it on its disk.`}
        detail="The partition is grown, then the filesystem. Nothing is unmounted and mail keeps running. This cannot be undone: a filesystem can be grown but not shrunk."
        confirmLabel="Extend now"
        variant="warn"
        onConfirm={() => apply(false)}
        onCancel={() => setConfirmOpen(false)}
      />

      {/* A second, deliberately separate confirmation. This one deletes a
          partition, and folding it into the ordinary Extend dialog would mean
          somebody could delete one while intending only to grow a disk. */}
      <ConfirmModal
        open={reclaimOpen}
        title="Free the reserved partition?"
        message={`${reserved} is a small partition the installer set aside for a future second server. It has never been used, and it is what stops ${target.mount} from growing.`}
        detail="It is deleted, then the disk is extended. The appliance checks first that it holds no filesystem, no volume group and no replication data, and refuses if it finds any. Building a high-availability pair later would need a small separate disk. This cannot be undone."
        confirmLabel="Free it and extend"
        variant="danger"
        onConfirm={() => apply(true)}
        onCancel={() => setReclaimOpen(false)}
      />

      {/* Something is in the way that we will not touch. Say which, and why,
          rather than leaving Check again to be pressed for ever. */}
      {reservedStuck && !working ? (
        <Result $tone="warn">
          {reservedStuck} sits after this partition and is in use, so this disk cannot
          be extended from here.
        </Result>
      ) : null}
    </TargetBlock>
  );
}

export default function DiskExtend({
  onGrew,
  mailboxName = "",
}: {
  onGrew: () => void;
  /** Hostname of the mailbox node, or "" on a single appliance. Passed down
      from the page, which already knows the deployment, rather than fetched:
      a disk control must not depend on Prometheus or on a second round trip
      to tell it how many machines there are. */
  mailboxName?: string;
}) {
  const [busy, setBusy] = useState(false);
  const groups: { heading: string; targets: Target[] }[] = mailboxName
    ? [
        { heading: "This server", targets: LOCAL_TARGETS.filter((t) => t.id === "system") },
        { heading: mailboxName, targets: MAILBOX_TARGETS },
      ]
    : [{ heading: "", targets: LOCAL_TARGETS }];
  return (
    <Wrap>
      <Head>
        <Name>Extend a disk</Name>
        <Blurb>
          After enlarging a disk in the hypervisor, add the new space here. The
          appliance checks first and says what it would do before doing it.
        </Blurb>
      </Head>
      {groups.map((g) => (
        <div key={g.heading || "one"}>
          {g.heading ? <GroupName>{g.heading}</GroupName> : null}
          {g.targets.map((t) => (
            <TargetRow
              key={t.key}
              target={t}
              busy={busy}
              onBusyChange={setBusy}
              onGrew={onGrew}
            />
          ))}
        </div>
      ))}
      <Hint style={{ marginTop: "0.75rem" }}>
        Only the last partition on a disk can be extended, and only into space
        that follows it. If the appliance refuses, it will say why rather than
        guess.
      </Hint>
    </Wrap>
  );
}
