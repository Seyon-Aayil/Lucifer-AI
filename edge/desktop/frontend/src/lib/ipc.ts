// Thin typed wrapper around `@tauri-apps/api`'s `invoke()`. The shapes mirror
// `lucifer_desktop_bindings::types`. Falls back to a stub when the page is
// loaded outside Tauri (e.g. `vite preview` in a normal browser) so the dev
// loop stays usable.

import { invoke as tauriInvoke } from "@tauri-apps/api/core";

export type ConnectArgs = {
  masterEndpoint: string;
  deviceId: string;
  clientCertPath: string;
  clientKeyPath: string;
  caCertPath: string;
  jwt: string;
  sniOverride?: string | null;
};

export type SubgraphSummary = {
  node_count: number;
  edge_count: number;
  generated_at: number;
  manifest_hash: string;
  is_full_sync: boolean;
};

export type TelemetryEvent = {
  eventType: string;
  timestampMs: number;
  attributes: Record<string, unknown>;
};

const inTauri =
  typeof window !== "undefined" &&
  // @ts-expect-error — Tauri injects this at runtime
  typeof window.__TAURI__ !== "undefined";

async function invoke<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
  if (!inTauri) {
    // Browser fallback — return predictable mock data so the UI can render.
    return mockResponse<T>(cmd);
  }
  return tauriInvoke<T>(cmd, args);
}

function mockResponse<T>(cmd: string): T {
  switch (cmd) {
    case "is_connected":
      return false as unknown as T;
    case "get_hot_subgraph":
      return {
        node_count: 0,
        edge_count: 0,
        generated_at: 0,
        manifest_hash: "mock",
        is_full_sync: false,
      } as unknown as T;
    case "edge_store_stats":
      return { node_count: 0, edge_count: 0, last_sync_at_ms: null } as unknown as T;
    case "offline_queue_stats":
      return { pending: 0, in_flight: 0, completed: 0, failed: 0 } as unknown as T;
    case "list_pending_actions":
      return [] as unknown as T;
    case "list_nodes_by_type":
      return [] as unknown as T;
    case "local_backend":
      return "ollama" as unknown as T;
    default:
      return undefined as unknown as T;
  }
}

export type EdgeStoreStats = {
  node_count: number;
  edge_count: number;
  last_sync_at_ms: number | null;
};

export type OfflineQueueStats = {
  pending: number;
  in_flight: number;
  completed: number;
  failed: number;
};

export const ipc = {
  connectMaster: (args: ConnectArgs) => invoke<void>("connect_master", { args }),
  disconnect:    () => invoke<void>("disconnect"),
  isConnected:   () => invoke<boolean>("is_connected"),
  getHotSubgraph: (deviceId: string, lastSyncAtMs: number) =>
    invoke<SubgraphSummary>("get_hot_subgraph", { deviceId, lastSyncAtMs }),
  pushTelemetry: (events: TelemetryEvent[]) =>
    invoke<number>("push_telemetry", { events }),

  edgeStoreStats:   () => invoke<EdgeStoreStats>("edge_store_stats"),
  offlineQueueStats: () => invoke<OfflineQueueStats>("offline_queue_stats"),
  enqueueOfflineAction: (actionType: string, payload: string) =>
    invoke<string>("enqueue_offline_action", { actionType, payload }),

  listPendingActions: (limit: number) =>
    invoke<PendingAction[]>("list_pending_actions", { limit }),
  markActionCompleted: (id: string) =>
    invoke<void>("mark_action_completed", { id }),
  markActionFailed: (id: string, error: string) =>
    invoke<string>("mark_action_failed", { id, error }),

  listNodesByType: (nodeType: string, limit: number) =>
    invoke<EdgeNode[]>("list_nodes_by_type", { nodeType, limit }),

  localBackend: () => invoke<string>("local_backend"),
  localGenerate: (model: string, prompt: string) =>
    invoke<string>("local_generate", { model, prompt }),

  inTauri,
};

export type PendingAction = {
  id: string;
  action_type: string;
  queued_at: number;
  attempt_count: number;
  status: "pending" | "in_flight" | "completed" | "failed";
  last_error: string | null;
};

export type EdgeNode = {
  node_id: string;
  node_type: string;
  classification: string;
  payload: number[]; // bytes
  updated_at: number;
  source_agent: string;
};
