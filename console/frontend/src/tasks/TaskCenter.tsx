import { useEffect, useState } from "react";
import styled from "@emotion/styled";
import { keyframes } from "@emotion/react";
import { theme } from "../styles/theme";
import { Dropdown } from "../ui";
import { useTasks } from "./TaskProvider";
import { formatElapsed, taskBadgeTone, taskElapsedMs, type Task } from "./taskStore";
import type { Alert } from "./alerts";

const spin = keyframes`to { transform: rotate(360deg); }`;

const Wrap = styled.div`
  position: relative;
  display: inline-flex;
`;

const IconBtn = styled.button<{ $tone: "running" | "failed" | "done" | "idle" }>`
  position: relative;
  border: 1px solid ${theme.line};
  background: ${theme.bgElev};
  border-radius: ${theme.radius.md};
  width: 2.25rem;
  height: 2.25rem;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  color: ${(p) =>
    p.$tone === "failed"
      ? theme.danger
      : p.$tone === "running"
        ? theme.accent
        : p.$tone === "done"
          ? theme.ok
          : theme.surface[500]};
  transition: background ${theme.motion.fast};

  &:hover {
    background: ${theme.surface[50]};
  }

  svg {
    width: 1.15rem;
    height: 1.15rem;
  }
`;

const Badge = styled.span<{ $tone: "running" | "failed" | "done" }>`
  position: absolute;
  top: -0.3rem;
  right: -0.3rem;
  min-width: 1.05rem;
  height: 1.05rem;
  padding: 0 0.25rem;
  border-radius: ${theme.radius.full};
  background: ${(p) =>
    p.$tone === "failed" ? theme.danger : p.$tone === "running" ? theme.accent : theme.ok};
  color: #fff;
  font-size: 0.65rem;
  font-weight: 700;
  line-height: 1.05rem;
  text-align: center;
`;

const Spinner = styled.span`
  width: 0.85rem;
  height: 0.85rem;
  border-radius: ${theme.radius.full};
  border: 2px solid ${theme.surface[300]};
  border-top-color: ${theme.accent};
  animation: ${spin} 800ms linear infinite;
  flex-shrink: 0;
`;

const Dot = styled.span<{ $tone: "done" | "failed" }>`
  width: 0.55rem;
  height: 0.55rem;
  border-radius: ${theme.radius.full};
  background: ${(p) => (p.$tone === "failed" ? theme.danger : theme.ok)};
  flex-shrink: 0;
`;

const PanelHead = styled.div`
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  padding: 0.6rem 0.85rem;
  border-bottom: 1px solid ${theme.line};
  font-size: 0.78rem;
  font-weight: 650;
  color: ${theme.surface[600]};
`;

const ClearBtn = styled.button`
  border: 0;
  background: transparent;
  color: ${theme.accent};
  font: inherit;
  font-size: 0.72rem;
  font-weight: 600;
  cursor: pointer;
  padding: 0;
`;

const Row = styled.div`
  display: flex;
  align-items: flex-start;
  gap: 0.55rem;
  padding: 0.6rem 0.85rem;
  border-bottom: 1px solid ${theme.line};

  &:last-of-type {
    border-bottom: 0;
  }
`;

const RowBody = styled.div`
  min-width: 0;
  flex: 1;
`;

const RowTitle = styled.p`
  margin: 0;
  font-size: 0.82rem;
  font-weight: 600;
  color: ${theme.surface[800]};
`;

const RowDetail = styled.p<{ $danger?: boolean }>`
  margin: 0.15rem 0 0;
  font-size: 0.73rem;
  line-height: 1.4;
  color: ${(p) => (p.$danger ? theme.danger : theme.muted)};
  word-break: break-word;
`;

const Empty = styled.p`
  margin: 0;
  padding: 0.9rem 0.85rem;
  font-size: 0.78rem;
  color: ${theme.muted};
`;

const Scroll = styled.div`
  max-height: 22rem;
  overflow-y: auto;
`;

function TasksIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M9 5h10M9 12h10M9 19h10"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
      />
      <path
        d="M3.5 5.2l1.2 1.2L7 4M3.5 12.2l1.2 1.2L7 11M3.5 19.2l1.2 1.2L7 18"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function BellIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M18 8.5a6 6 0 10-12 0c0 5-2 6.5-2 6.5h16s-2-1.5-2-6.5z"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinejoin="round"
      />
      <path d="M10.3 19a2 2 0 003.4 0" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
    </svg>
  );
}

export type { Alert } from "./alerts";

function TaskRow({ task, now }: { task: Task; now: number }) {
  return (
    <Row>
      {task.state === "running" ? (
        <Spinner />
      ) : (
        <Dot $tone={task.state === "failed" ? "failed" : "done"} style={{ marginTop: "0.28rem" }} />
      )}
      <RowBody>
        <RowTitle>{task.title}</RowTitle>
        {task.detail ? <RowDetail>{task.detail}</RowDetail> : null}
        <RowDetail $danger={task.state === "failed"}>
          {task.state === "running"
            ? `Running for ${formatElapsed(taskElapsedMs(task, now))}`
            : `${task.state === "failed" ? "Failed" : "Done"} in ${formatElapsed(taskElapsedMs(task, now))}${
                task.message ? ` - ${task.message}` : ""
              }`}
        </RowDetail>
      </RowBody>
    </Row>
  );
}

export function TaskCenter({ alerts }: { alerts: Alert[] }) {
  const { tasks, clearDone } = useTasks();
  const [tasksOpen, setTasksOpen] = useState(false);
  const [alertsOpen, setAlertsOpen] = useState(false);
  const [now, setNow] = useState(() => Date.now());

  const running = tasks.some((t) => t.state === "running");
  useEffect(() => {
    if (!running) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [running]);

  const tone = taskBadgeTone(tasks);
  const runningN = tasks.filter((t) => t.state === "running").length;
  const failedN = tasks.filter((t) => t.state === "failed").length;
  const badgeCount = failedN || runningN;
  const badgeTone = failedN ? "failed" : runningN ? "running" : "done";

  return (
    <>
      <Wrap>
        <IconBtn
          type="button"
          $tone={alerts.length ? (alerts.some((a) => a.severity === "danger") ? "failed" : "running") : "idle"}
          aria-label={alerts.length ? `${alerts.length} cluster alert(s)` : "No cluster alerts"}
          aria-haspopup="menu"
          aria-expanded={alertsOpen}
          onClick={() => {
            setAlertsOpen((v) => !v);
            setTasksOpen(false);
          }}
        >
          <BellIcon />
          {alerts.length ? (
            <Badge $tone={alerts.some((a) => a.severity === "danger") ? "failed" : "running"}>
              {alerts.length}
            </Badge>
          ) : null}
        </IconBtn>
        <Dropdown open={alertsOpen} onClose={() => setAlertsOpen(false)} align="right">
          <PanelHead>Cluster alerts</PanelHead>
          <Scroll>
            {alerts.length === 0 ? (
              <Empty>Nothing needs attention right now.</Empty>
            ) : (
              alerts.map((a) => (
                <Row key={a.id}>
                  <Dot $tone={a.severity === "danger" ? "failed" : "done"} style={{ marginTop: "0.28rem" }} />
                  <RowBody>
                    <RowTitle>{a.title}</RowTitle>
                    {a.detail ? <RowDetail $danger={a.severity === "danger"}>{a.detail}</RowDetail> : null}
                  </RowBody>
                </Row>
              ))
            )}
          </Scroll>
        </Dropdown>
      </Wrap>

      <Wrap>
        <IconBtn
          type="button"
          $tone={tone}
          aria-label={runningN ? `${runningN} task(s) running` : "Tasks"}
          aria-haspopup="menu"
          aria-expanded={tasksOpen}
          onClick={() => {
            setTasksOpen((v) => !v);
            setAlertsOpen(false);
          }}
        >
          <TasksIcon />
          {badgeCount ? <Badge $tone={badgeTone}>{badgeCount}</Badge> : null}
        </IconBtn>
        <Dropdown open={tasksOpen} onClose={() => setTasksOpen(false)} align="right">
          <PanelHead>
            <span>Tasks</span>
            {tasks.some((t) => t.state !== "running") ? (
              <ClearBtn type="button" onClick={clearDone}>
                Clear finished
              </ClearBtn>
            ) : null}
          </PanelHead>
          <Scroll>
            {tasks.length === 0 ? (
              <Empty>No tasks yet this session.</Empty>
            ) : (
              tasks.map((t) => <TaskRow key={t.id} task={t} now={now} />)
            )}
          </Scroll>
        </Dropdown>
      </Wrap>
    </>
  );
}
