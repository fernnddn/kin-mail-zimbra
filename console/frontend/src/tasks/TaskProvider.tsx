import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import {
  addTask,
  clearFinished,
  endTask,
  nextTaskId,
  type Task,
  type TaskState,
} from "./taskStore";

type TaskApi = {
  tasks: Task[];
  /** Register a task and get its id back so the caller can end it. */
  startTask: (title: string, detail?: string) => string;
  endTaskWith: (id: string, state: Exclude<TaskState, "running">, message?: string) => void;
  clearDone: () => void;
};

const TaskCtx = createContext<TaskApi | null>(null);

export function TaskProvider({ children }: { children: ReactNode }) {
  const [tasks, setTasks] = useState<Task[]>([]);

  const startTask = useCallback((title: string, detail?: string) => {
    const id = nextTaskId();
    setTasks((prev) =>
      addTask(prev, { id, title, detail, state: "running", startedAt: Date.now() }),
    );
    return id;
  }, []);

  const endTaskWith = useCallback(
    (id: string, state: Exclude<TaskState, "running">, message?: string) => {
      setTasks((prev) => endTask(prev, id, state, message));
    },
    [],
  );

  const clearDone = useCallback(() => setTasks((prev) => clearFinished(prev)), []);

  const value = useMemo(
    () => ({ tasks, startTask, endTaskWith, clearDone }),
    [tasks, startTask, endTaskWith, clearDone],
  );
  return <TaskCtx.Provider value={value}>{children}</TaskCtx.Provider>;
}

/** Safe outside a provider (login screen, wizard) - returns inert no-ops. */
export function useTasks(): TaskApi {
  const ctx = useContext(TaskCtx);
  if (ctx) return ctx;
  return {
    tasks: [],
    startTask: () => "",
    endTaskWith: () => undefined,
    clearDone: () => undefined,
  };
}
