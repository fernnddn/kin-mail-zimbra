import { useState } from "react";
import styled from "@emotion/styled";
import { useLocation } from "react-router-dom";
import { ConsoleChrome } from "../ConsoleChrome";
import { theme } from "../styles/theme";
import { Hint, Page, PageHeader, AuditIcon } from "../ui";
import { useTasks } from "../tasks/TaskProvider";
import { formatElapsed, taskElapsedMs, type Task } from "../tasks/taskStore";
import { useAlerts } from "../tasks/AlertProvider";
import type { Alert } from "../tasks/alerts";

/** Full history for the header dropdowns.
 *
 * The dropdowns answer "what is happening now" and deliberately drop anything
 * finished more than a day ago. This page is where the rest lives, so nothing
 * is thrown away silently.
 */

const Tabs = styled.div`
  display: flex;
  gap: 1.25rem;
  border-bottom: 1px solid ${theme.line};
  margin: 0 0 1.15rem;
`;

const TabBtn = styled.button<{ $on?: boolean }>`
  border: 0;
  background: transparent;
  padding: 0.45rem 0 0.7rem;
  margin-bottom: -1px;
  cursor: pointer;
  font: inherit;
  font-size: 0.875rem;
  font-weight: 600;
  color: ${(p) => (p.$on ? theme.accent : theme.surface[500])};
  border-bottom: 2px solid ${(p) => (p.$on ? theme.accent : "transparent")};
`;

const List = styled.div`
  border: 1px solid ${theme.line};
  background: ${theme.bgElev};
  border-radius: ${theme.radius.md};
  box-shadow: ${theme.shadow.sm};
  overflow: hidden;
`;

const Row = styled.div`
  display: flex;
  align-items: flex-start;
  gap: 0.7rem;
  padding: 0.85rem 1.1rem;
  border-bottom: 1px solid ${theme.line};

  &:last-of-type {
    border-bottom: 0;
  }
`;

const Dot = styled.span<{ $tone: "running" | "done" | "failed" | "resolved" }>`
  width: 0.6rem;
  height: 0.6rem;
  margin-top: 0.3rem;
  border-radius: ${theme.radius.full};
  flex-shrink: 0;
  background: ${(p) =>
    p.$tone === "failed"
      ? theme.danger
      : p.$tone === "running"
        ? theme.warn
        : p.$tone === "resolved"
          ? theme.surface[400]
          : theme.accent};
`;

const Body = styled.div`
  min-width: 0;
  flex: 1;
`;

const Title = styled.p<{ $muted?: boolean }>`
  margin: 0;
  font-size: 0.9rem;
  font-weight: 600;
  color: ${(p) => (p.$muted ? theme.muted : theme.surface[800])};
  text-decoration: ${(p) => (p.$muted ? "line-through" : "none")};
`;

const Meta = styled.p<{ $danger?: boolean }>`
  margin: 0.2rem 0 0;
  font-size: 0.78rem;
  line-height: 1.45;
  color: ${(p) => (p.$danger ? theme.danger : theme.muted)};
  word-break: break-word;
`;

const When = styled.span`
  font-size: 0.75rem;
  color: ${theme.surface[400]};
  white-space: nowrap;
  flex-shrink: 0;
`;

const Empty = styled.p`
  margin: 0;
  padding: 2rem 1.1rem;
  text-align: center;
  font-size: 0.85rem;
  color: ${theme.muted};
`;

function TaskRow({ task }: { task: Task }) {
  const now = Date.now();
  const ended = task.endedAt ? new Date(task.endedAt) : null;
  return (
    <Row>
      <Dot $tone={task.state} />
      <Body>
        <Title>{task.title}</Title>
        {task.detail ? <Meta>{task.detail}</Meta> : null}
        <Meta $danger={task.state === "failed"}>
          {task.state === "running"
            ? `Running for ${formatElapsed(taskElapsedMs(task, now))}`
            : `${task.state === "failed" ? "Failed" : "Done"} in ${formatElapsed(
                taskElapsedMs(task, now),
              )}${task.message ? ` - ${task.message}` : ""}`}
        </Meta>
      </Body>
      <When>
        {ended
          ? ended.toLocaleString([], {
              month: "short",
              day: "numeric",
              hour: "2-digit",
              minute: "2-digit",
            })
          : "now"}
      </When>
    </Row>
  );
}

function AlertRow({ alert }: { alert: Alert }) {
  const resolved = alert.severity === "resolved";
  return (
    <Row>
      <Dot $tone={resolved ? "resolved" : alert.severity === "danger" ? "failed" : "running"} />
      <Body>
        <Title $muted={resolved}>{alert.title}</Title>
        {resolved ? (
          <Meta>Cleared</Meta>
        ) : alert.detail ? (
          <Meta $danger={alert.severity === "danger"}>{alert.detail}</Meta>
        ) : null}
      </Body>
    </Row>
  );
}

export default function ActivityCenterPage() {
  const { tasks } = useTasks();
  const alerts = useAlerts();
  const location = useLocation();
  const [tab, setTab] = useState<"tasks" | "alerts">(
    location.hash === "#alerts" ? "alerts" : "tasks",
  );

  return (
    <ConsoleChrome subtitle="Activity">
      <Page $wide>
        <PageHeader
          icon={<AuditIcon />}
          title="Tasks and alerts"
          subtitle="Everything from this session, including tasks the header dropdown no longer shows."
        />
        <Tabs>
          <TabBtn type="button" $on={tab === "tasks"} onClick={() => setTab("tasks")}>
            Tasks ({tasks.length})
          </TabBtn>
          <TabBtn type="button" $on={tab === "alerts"} onClick={() => setTab("alerts")}>
            Cluster alerts ({alerts.length})
          </TabBtn>
        </Tabs>
        <List>
          {tab === "tasks" ? (
            tasks.length ? (
              tasks.map((t) => <TaskRow key={t.id} task={t} />)
            ) : (
              <Empty>No tasks have run in this session yet.</Empty>
            )
          ) : alerts.length ? (
            alerts.map((a) => <AlertRow key={a.id} alert={a} />)
          ) : (
            <Empty>Nothing needs attention right now.</Empty>
          )}
        </List>
        <Hint>
          Tasks are kept for this browser session only. The Cluster page Activity log holds the
          full command output.
        </Hint>
      </Page>
    </ConsoleChrome>
  );
}
