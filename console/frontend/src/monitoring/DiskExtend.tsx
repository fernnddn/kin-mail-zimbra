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

const Row = styled.div`
  display: flex;
  align-items: center;
  gap: 0.7rem;
  flex-wrap: wrap;
  padding: 0.75rem 0.85rem;
  border: 1px solid ${theme.line};
  border-radius: ${theme.radius.md};
  background: ${theme.bgElev};

  & + & {
    margin-top: 0.5rem;
  }
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
  margin: 0.5rem 0 0;
  padding: 0.6rem 0.75rem;
  border-left: 3px solid
    ${(p) => (p.$tone === "ok" ? theme.ok : p.$tone === "warn" ? theme.warn : theme.line)};
  background: ${theme.bgElev};
  font-size: 0.85rem;
  line-height: 1.5;
  white-space: pre-wrap;
`;

const Working = styled.div`
  animation: ${panelEnter} ${theme.motion.slow} ${theme.motion.ease.standard} both;
  display: grid;
  gap: 0.45rem;
  margin: 0.5rem 0 0;
  color: ${theme.surface[600]};
  font-size: 0.85rem;
`;

type Target = { id: "system" | "mail"; name: string; mount: string; blurb: string };

const TARGETS: Target[] = [
  {
    id: "system",
    name: "System disk",
    mount: "/",
    blurb: "The operating system, the console and the logs.",
  },
  {
    id: "mail",
    name: "Mail storage",
    mount: "/opt/zimbra",
    blurb: "Mailboxes, indexes and the mail queue.",
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

  const stream = useCallback(
    (op: "plan" | "apply", onEnd: (code: number, text: string) => void) => {
      let text = "";
      const es = new EventSource(
        `/api/wizard/deploy/stream?action=grow_disk&op=${op}&target=${target.id}`,
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

  const apply = useCallback(() => {
    setConfirmOpen(false);
    setPhase("applying");
    onBusyChange(true);
    stream("apply", (code) => {
      onBusyChange(false);
      setPhase(code === 0 ? "done" : "failed");
      if (code === 0) onGrew();
    });
  }, [stream, onBusyChange, onGrew]);

  const free = freeBytes(log);
  const summary = readable(log);
  const working = phase === "planning" || phase === "applying";

  return (
    <>
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
        onConfirm={apply}
        onCancel={() => setConfirmOpen(false)}
      />
    </>
  );
}

export default function DiskExtend({ onGrew }: { onGrew: () => void }) {
  const [busy, setBusy] = useState(false);
  return (
    <Wrap>
      <Head>
        <Name>Extend a disk</Name>
        <Blurb>
          After enlarging a disk in the hypervisor, add the new space here. The
          appliance checks first and says what it would do before doing it.
        </Blurb>
      </Head>
      {TARGETS.map((t) => (
        <TargetRow
          key={t.id}
          target={t}
          busy={busy}
          onBusyChange={setBusy}
          onGrew={onGrew}
        />
      ))}
      <Hint>
        Only the last partition on a disk can be extended, and only into space
        that follows it. If the appliance refuses, it will say why rather than
        guess.
      </Hint>
    </Wrap>
  );
}
