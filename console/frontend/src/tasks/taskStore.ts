/** Task centre state: what is running, what finished, what failed.
 *
 * Every long-running console action (preflight, enter/exit maintenance,
 * fail-count cleanup, move master, remove host, add/remove observability)
 * registers here so the header can answer "is something running, and did the
 * last thing work?" without the operator having to sit on the Activity log.
 *
 * Phase 4-1 feedback: pressing Check on a node gave no sign it was doing
 * anything, and Move Master finished with only a cryptic line at the bottom of
 * a log the operator had to go find. Both are the same missing signal.
 *
 * State lives in the browser for the session. Tasks do not survive a reload -
 * the privhelper has no job registry to reconcile against, so inventing
 * durable client-side history would just be a lie about what we know.
 */

export type TaskState = "running" | "done" | "failed";

export type Task = {
  id: string;
  title: string;
  /** Short context line, e.g. the node the action targets. */
  detail?: string;
  state: TaskState;
  startedAt: number;
  endedAt?: number;
  /** Result message shown when the task ends. */
  message?: string;
};

/** Newest first, and never unbounded: a long session must not grow forever. */
export const TASK_HISTORY_LIMIT = 25;

let seq = 0;

export function nextTaskId(): string {
  seq += 1;
  return `task-${seq}-${Date.now()}`;
}

export function addTask(tasks: Task[], task: Task): Task[] {
  return [task, ...tasks].slice(0, TASK_HISTORY_LIMIT);
}

export function endTask(
  tasks: Task[],
  id: string,
  state: Exclude<TaskState, "running">,
  message?: string,
): Task[] {
  return tasks.map((t) =>
    t.id === id && t.state === "running"
      ? { ...t, state, message, endedAt: Date.now() }
      : t,
  );
}

export function clearFinished(tasks: Task[]): Task[] {
  return tasks.filter((t) => t.state === "running");
}

export function runningCount(tasks: Task[]): number {
  return tasks.filter((t) => t.state === "running").length;
}

export function failedCount(tasks: Task[]): number {
  return tasks.filter((t) => t.state === "failed").length;
}

/** Badge tone for the header icon: red beats spinning beats green. */
export function taskBadgeTone(tasks: Task[]): "running" | "failed" | "done" | "idle" {
  if (failedCount(tasks) > 0) return "failed";
  if (runningCount(tasks) > 0) return "running";
  if (tasks.length > 0) return "done";
  return "idle";
}

export function taskElapsedMs(task: Task, now: number): number {
  return Math.max(0, (task.endedAt ?? now) - task.startedAt);
}

export function formatElapsed(ms: number): string {
  const secs = Math.round(ms / 1000);
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  const rem = secs % 60;
  return rem ? `${mins}m ${rem}s` : `${mins}m`;
}
