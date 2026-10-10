import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { Dashboard } from "./types";

// Polling reads persisted backend state, so a page refresh can resume tracking a job.
export function useDashboard() {
  const [data, setData] = useState<Dashboard | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const controller = useRef<AbortController | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const alive = useRef(false);
  const refresh = useCallback(async function load(): Promise<void> {
    clearTimeout(timer.current);
    controller.current?.abort();
    const current = new AbortController();
    controller.current = current;
    let busy = false;
    try {
      const next = await api.dashboard(current.signal);
      if (current.signal.aborted || !alive.current) return;
      busy = next.status.busy;
      setData(next);
      setError("");
    } catch (exc) {
      if (current.signal.aborted || !alive.current) return;
      setError(
        exc instanceof TypeError
          ? "无法连接后端，请确认服务已启动。"
          : (exc as Error).message,
      );
    } finally {
      if (!current.signal.aborted && alive.current) {
        setLoading(false);
        timer.current = setTimeout(() => void load(), busy ? 1500 : 10000);
      }
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    void refresh();
    return () => {
      alive.current = false;
      controller.current?.abort();
      clearTimeout(timer.current);
    };
  }, [refresh]);
  return { data, error, loading, refresh };
}
