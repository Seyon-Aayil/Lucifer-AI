// Thin typed wrapper around `@tauri-apps/api`'s `invoke()`. The shapes mirror
// `lucifer_desktop_bindings::types`. Falls back to a stub when the page is
// loaded outside Tauri (e.g. `vite preview` in a normal browser) so the dev
// loop stays usable.

import { Channel, invoke as tauriInvoke } from "@tauri-apps/api/core";

export type ConnectArgs = {
  masterEndpoint: string;
  deviceId: string;
  clientCertPath: string;
  clientKeyPath: string;
  caCertPath: string;
  jwt: string;
  sniOverride?: string | null;
  /** Refresh token from /devices/pair. When supplied, edge auto-rotates JWT. */
  refreshToken?: string | null;
  /** HTTP base URL for /auth/token/refresh. Defaults to masterEndpoint. */
  httpMasterEndpoint?: string | null;
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
    case "list_conversations":
      return [] as unknown as T;
    case "list_messages":
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
  pushTelemetry: (deviceId: string, events: TelemetryEvent[]) =>
    invoke<number>("push_telemetry", { deviceId, events }),

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

  listConversations: (limit: number) =>
    invoke<StoredConversation[]>("list_conversations", { limit }),
  createConversation: (id: string, title: string) =>
    invoke<void>("create_conversation", { id, title }),
  appendMessage: (message: StoredMessage) =>
    invoke<void>("append_message", { message }),
  listMessages: (conversationId: string, limit: number) =>
    invoke<StoredMessage[]>("list_messages", { conversationId, limit }),
  deleteConversation: (id: string) =>
    invoke<void>("delete_conversation", { id }),

  persistPairingBundle: (bundle: PairingBundle) =>
    invoke<PersistedCredentials>("persist_pairing_bundle", { bundle }),
  readStoredJwt: (deviceId: string) =>
    invoke<string | null>("read_stored_jwt", { deviceId }),
  forgetDevice: (deviceId: string) =>
    invoke<void>("forget_device", { deviceId }),

  localBackend: () => invoke<string>("local_backend"),
  localGenerate: (model: string, prompt: string) =>
    invoke<string>("local_generate", { model, prompt }),

  /**
   * Streaming variant of `local_generate`. Returns a promise that resolves
   * once the backend signals `done = true` (or rejects on error). The
   * `onChunk` callback fires for every token batch received.
   */
  localGenerateStream(
    model: string,
    prompt: string,
    onChunk: (chunk: { text: string; done: boolean; eval_count: number | null }) => void,
  ): Promise<void> {
    if (!inTauri) {
      // Browser fallback emits a single mock chunk and resolves.
      onChunk({ text: "(mock stream)", done: true, eval_count: null });
      return Promise.resolve();
    }
    return new Promise<void>((resolve, reject) => {
      const channel = new Channel<{ text: string; done: boolean; eval_count: number | null }>();
      let done = false;
      channel.onmessage = (msg) => {
        onChunk(msg);
        if (msg.done) {
          done = true;
        }
      };
      tauriInvoke<void>("local_generate_stream", { model, prompt, channel })
        .then(() => {
          if (!done) {
            // Emit a synthetic terminal chunk so callers don't hang.
            onChunk({ text: "", done: true, eval_count: null });
          }
          resolve();
        })
        .catch(reject);
    });
  },

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

export type PairingBundle = {
  device_id: string;
  jwt: string;
  refresh_token?: string | null;
  client_cert_pem_b64: string;
  client_key_pem_b64: string;
  ca_cert_pem_b64: string;
};

export type PersistedCredentials = {
  device_id: string;
  client_cert_path: string;
  client_key_path: string;
  ca_cert_path: string;
  jwt_in_keychain: boolean;
};

export type StoredConversation = {
  id: string;
  title: string;
  created_at: number;
  updated_at: number;
  message_count: number;
};

export type StoredMessage = {
  id: string;
  conversation_id: string;
  role: "user" | "assistant" | "system" | "tool";
  content: string;
  agent_id?: string | null;
  model?: string | null;
  tokens?: number | null;
  created_at: number;
};

export type EdgeNode = {
  node_id: string;
  node_type: string;
  classification: string;
  payload: number[]; // bytes
  updated_at: number;
  source_agent: string;
};
