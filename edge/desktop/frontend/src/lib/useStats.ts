import { useEffect, useState } from "react";
import { EdgeStoreStats, OfflineQueueStats, ipc } from "./ipc";

export type LiveStats = {
  store: EdgeStoreStats | null;
  queue: OfflineQueueStats | null;
  connected: boolean;
};

/** Polls IPC for live stats every `intervalMs`. */
export function useStats(intervalMs = 2000): LiveStats {
  const [stats, setStats] = useState<LiveStats>({
    store: null,
    queue: null,
    connected: false,
  });

  useEffect(() => {
    let cancelled = false;
    async function tick() {
      try {
        const [connected, store, queue] = await Promise.all([
          ipc.isConnected(),
          ipc.edgeStoreStats().catch(() => null),
          ipc.offlineQueueStats().catch(() => null),
        ]);
        if (!cancelled) setStats({ connected, store, queue });
      } catch {
        // Swallow — UI will keep showing previous values.
      }
    }
    tick();
    const id = setInterval(tick, intervalMs);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [intervalMs]);

  return stats;
}
