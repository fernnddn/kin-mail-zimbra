import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import styled from "@emotion/styled";
import { keyframes } from "@emotion/react";
import { theme } from "../styles/theme";
import { Dropdown } from "../ui";
import { useTasks } from "./TaskProvider";
import {
  dropdownTasks,
  formatElapsed,
  hasMoreThanDropdown,
  taskBadgeTone,
  taskElapsedMs,
  type Task,
} from "./taskStore";
import { activeAlertCount, type Alert } from "./alerts";

const spin = keyframes`to { transform: rotate(360deg); }`;

const Wrap = styled.div<{ $rail?: boolean }>`
  position: relative;
  display: inline-flex;
  ${(p) => (p.$rail ? "flex: 1 1 0; min-width: 0;" : "")}
`;

/* Two presentations, one behaviour. On the ops rail these are full-width rows
   on charcoal; anywhere else they stay the bordered icon buttons they were.
   Nothing below this line changes what they do. */
const IconBtn = styled.button<{
  $tone: "running" | "failed" | "done" | "idle";
  $rail?: boolean;
}>`
  position: relative;
  border: ${(p) => (p.$rail ? "0" : `1px solid ${theme.line}`)};
  border-left: ${(p) => (p.$rail ? "2px solid transparent" : "")};
  background: transparent;
  border-radius: ${(p) => (p.$rail ? "0" : theme.radius.sm)};
  width: ${(p) => (p.$rail ? "100%" : "2.25rem")};
  height: ${(p) => (p.$rail ? "2.5rem" : "2.25rem")};
  padding: ${(p) => (p.$rail ? "0 1.25rem" : "0")};
  gap: ${(p) => (p.$rail ? "0.6rem" : "0")};
  font: inherit;
  font-size: 0.85rem;
  display: inline-flex;
  align-items: center;
  justify-content: ${(p) => (p.$rail ? "flex-start" : "center")};
  cursor: pointer;
  /* In progress reads ochre, a failure reads oxide, and a finished job is
     quiet. On the rail the resting state is paper at reduced strength. */
  color: ${(p) =>
    p.$tone === "failed"
      ? p.$rail
        ? theme.oxideOnRail
        : theme.danger
      : p.$tone === "running"
        ? p.$rail
          ? theme.warnOnRail
          : theme.warn
        : p.$rail
          ? "rgba(244, 241, 234, 0.72)"
          : theme.surface[500]};
  transition:
    background ${theme.motion.fast} ease-out,
    color ${theme.motion.fast} ease-out;

  &:hover {
    background: ${(p) => (p.$rail ? "rgba(255, 255, 255, 0.05)" : theme.surface[100])};
    color: ${(p) => (p.$rail ? theme.paper : "inherit")};
  }

  svg {
    width: 1.05rem;
    height: 1.05rem;
    flex-shrink: 0;
  }
`;

const RailLabel = styled.span`
  flex: 1 1 auto;
  text-align: left;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
`;

const Badge = styled.span<{ $tone: "running" | "failed" | "done"; $rail?: boolean }>`
  position: ${(p) => (p.$rail ? "static" : "absolute")};
  top: -0.3rem;
  right: -0.3rem;
  min-width: 1.05rem;
  height: 1.05rem;
  padding: 0 0.25rem;
  border-radius: ${theme.radius.full};
  background: ${(p) =>
    p.$tone === "failed" ? theme.oxide : p.$tone === "running" ? theme.warn : theme.ink};
  color: ${theme.paper};
  font-size: 0.65rem;
  font-weight: 700;
  line-height: 1.05rem;
  text-align: center;
`;

const Spinner = styled.span`
  width: 0.85rem;
  height: 0.85rem;
  /* radius.full is 2px on this theme; a spinner has to opt out of that. */
  border-radius: 50%;
  border: 2px solid ${theme.surface[300]};
  border-top-color: ${theme.warn};
  animation: ${spin} 800ms linear infinite;
  flex-shrink: 0;
`;

const Dot = styled.span<{ $tone: "done" | "failed" | "warn" }>`
  width: 0.55rem;
  height: 0.55rem;
  border-radius: ${theme.radius.full};
  background: ${(p) =>
    p.$tone === "failed" ? theme.danger : p.$tone === "warn" ? theme.warn : theme.accent};
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

const RowTitle = styled.p<{ $resolved?: boolean }>`
  margin: 0;
  font-size: 0.82rem;
  font-weight: 600;
  color: ${(p) => (p.$resolved ? theme.muted : theme.surface[800])};
  text-decoration: ${(p) => (p.$resolved ? "line-through" : "none")};
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

const SeeAll = styled.button`
  display: block;
  width: 100%;
  border: 0;
  border-top: 1px solid ${theme.line};
  background: transparent;
  color: ${theme.accent};
  font: inherit;
  font-size: 0.76rem;
  font-weight: 650;
  padding: 0.55rem 0.85rem;
  cursor: pointer;
  text-align: left;

  &:hover {
    background: ${theme.surface[50]};
  }
`;

const Scroll = styled.div`
  /* Capped so a burst of tasks cannot run the panel off the screen; the
     rest is one click away on the full page. */
  max-height: 15rem;
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

export function TaskCenter({
  alerts,
  rail = false,
}: {
  alerts: Alert[];
  /** Presentation only: full-width rows on the charcoal ops rail. */
  rail?: boolean;
}) {
  const { tasks, clearDone } = useTasks();
  const navigate = useNavigate();
  const [tasksOpen, setTasksOpen] = useState(false);
  const [alertsOpen, setAlertsOpen] = useState(false);
  const [now, setNow] = useState(() => Date.now());

  const running = tasks.some((t) => t.state === "running");
  useEffect(() => {
    if (!running) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [running]);

  const visible = dropdownTasks(tasks, now);
  const more = hasMoreThanDropdown(tasks, now);
  const tone = taskBadgeTone(tasks);
  const runningN = tasks.filter((t) => t.state === "running").length;
  const failedN = tasks.filter((t) => t.state === "failed").length;
  const badgeCount = failedN || runningN;
  const badgeTone = failedN ? "failed" : runningN ? "running" : "done";

  return (
    <>
      <Wrap $rail={rail}>
        <IconBtn
          $rail={rail}
          type="button"
          $tone={
            activeAlertCount(alerts)
              ? alerts.some((a) => a.severity === "danger")
                ? "failed"
                : "running"
              : "idle"
          }
          aria-label={
            activeAlertCount(alerts)
              ? `${activeAlertCount(alerts)} cluster alert(s)`
              : "No cluster alerts"
          }
          aria-haspopup="menu"
          aria-expanded={alertsOpen}
          onClick={() => {
            setAlertsOpen((v) => !v);
            setTasksOpen(false);
          }}
        >
          <BellIcon />
          {rail ? <RailLabel>Alerts</RailLabel> : null}
          {activeAlertCount(alerts) ? (
            <Badge $rail={rail} $tone={alerts.some((a) => a.severity === "danger") ? "failed" : "running"}>
              {activeAlertCount(alerts)}
            </Badge>
          ) : null}
        </IconBtn>
        <Dropdown open={alertsOpen} onClose={() => setAlertsOpen(false)} align={rail ? "left" : "right"}>
          <PanelHead>Cluster alerts</PanelHead>
          <Scroll>
            {alerts.length === 0 ? (
              <Empty>Nothing needs attention right now.</Empty>
            ) : (
              alerts.map((a) => (
                <Row key={a.id}>
                  <Dot
                    $tone={
                      a.severity === "danger"
                        ? "failed"
                        : a.severity === "resolved"
                          ? "done"
                          : "warn"
                    }
                    style={{ marginTop: "0.28rem" }}
                  />
                  <RowBody>
                    <RowTitle $resolved={a.severity === "resolved"}>{a.title}</RowTitle>
                    {a.severity === "resolved" ? (
                      <RowDetail>Cleared</RowDetail>
                    ) : a.detail ? (
                      <RowDetail $danger={a.severity === "danger"}>{a.detail}</RowDetail>
                    ) : null}
                  </RowBody>
                </Row>
              ))
            )}
          </Scroll>
          <SeeAll
            type="button"
            onClick={() => {
              setAlertsOpen(false);
              navigate("/activity#alerts");
            }}
          >
            See all alerts
          </SeeAll>
        </Dropdown>
      </Wrap>

      <Wrap $rail={rail}>
        <IconBtn
          $rail={rail}
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
          {rail ? <RailLabel>Tasks</RailLabel> : null}
          {badgeCount ? <Badge $rail={rail} $tone={badgeTone}>{badgeCount}</Badge> : null}
        </IconBtn>
        <Dropdown open={tasksOpen} onClose={() => setTasksOpen(false)} align={rail ? "left" : "right"}>
          <PanelHead>
            <span>Tasks</span>
            {tasks.some((t) => t.state !== "running") ? (
              <ClearBtn type="button" onClick={clearDone}>
                Clear finished
              </ClearBtn>
            ) : null}
          </PanelHead>
          <Scroll>
            {visible.length === 0 ? (
              <Empty>
                {tasks.length
                  ? "Nothing running. Older tasks are on the Tasks and alerts page."
                  : "No tasks yet this session."}
              </Empty>
            ) : (
              visible.map((t) => <TaskRow key={t.id} task={t} now={now} />)
            )}
          </Scroll>
          {tasks.length ? (
            <SeeAll
              type="button"
              onClick={() => {
                setTasksOpen(false);
                navigate("/activity");
              }}
            >
              {more ? `See all ${tasks.length} tasks` : "See all tasks"}
            </SeeAll>
          ) : null}
        </Dropdown>
      </Wrap>
    </>
  );
}
