import { useEffect, useRef, useState } from "react";
import { agent } from "../lib/agents";
import { ipc, StoredConversation, StoredMessage } from "../lib/ipc";

type Msg = {
  id: string;
  role: "user" | "assistant";
  content: string;
  agent?: string;
  model?: string;
  pending?: boolean;
};

const DEFAULT_MODEL = "llama3.2";

function uuid(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `m-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function nowMs(): number {
  return Date.now();
}

function fromStored(m: StoredMessage): Msg {
  return {
    id: m.id,
    role: m.role === "assistant" ? "assistant" : "user",
    content: m.content,
    agent: m.agent_id ?? undefined,
    model: m.model ?? undefined,
  };
}

export function Conversation() {
  const [conversations, setConversations] = useState<StoredConversation[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [backend, setBackend] = useState("ollama");
  const [model, setModel] = useState(DEFAULT_MODEL);
  const [deviceId, setDeviceId] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [showInspector, setShowInspector] = useState(true);
  const tail = useRef<HTMLDivElement>(null);

  // ── Load backend + model preference + conversation list on mount ──────
  useEffect(() => {
    ipc.localBackend().then(setBackend).catch(() => {});
    ipc
      .getSettings()
      .then((s) => {
        setModel(s.default_model);
        setDeviceId(s.device_id);
      })
      .catch(() => {});
    refreshConversations();
  }, []);

  // Load messages whenever the active conversation changes
  useEffect(() => {
    if (!activeId) {
      setMessages([]);
      return;
    }
    ipc.listMessages(activeId, 200)
      .then((rows) => setMessages(rows.map(fromStored)))
      .catch((e) => setError(String(e)));
  }, [activeId]);

  useEffect(() => {
    tail.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function refreshConversations() {
    try {
      const rows = await ipc.listConversations(50);
      setConversations(rows);
      if (!activeId && rows.length) {
        setActiveId(rows[0].id);
      }
    } catch (e) {
      setError(String(e));
    }
  }

  async function newConversation() {
    const id = uuid();
    try {
      await ipc.createConversation(id, "New conversation");
      setActiveId(id);
      setMessages([]);
      await refreshConversations();
    } catch (e) {
      setError(String(e));
    }
  }

  async function deleteActive() {
    if (!activeId) return;
    if (!confirm("Delete this conversation? Messages cannot be recovered.")) return;
    try {
      await ipc.deleteConversation(activeId);
      setActiveId(null);
      await refreshConversations();
    } catch (e) {
      setError(String(e));
    }
  }

  async function send() {
    const prompt = input.trim();
    if (!prompt || busy) return;
    setError(null);
    setInput("");

    // Lazy-create the conversation on the first message.
    let conv = activeId;
    if (!conv) {
      conv = uuid();
      try {
        await ipc.createConversation(conv, prompt.slice(0, 64));
        setActiveId(conv);
      } catch (e) {
        setError(String(e));
        return;
      }
    }
    const target: string = conv;

    const userId = uuid();
    const assistantId = uuid();
    const userMsg: Msg = { id: userId, role: "user", content: prompt };
    const placeholder: Msg = {
      id: assistantId,
      role: "assistant",
      content: "",
      agent: "personal-agent",
      model: `${model} · ${backend}`,
      pending: true,
    };
    setMessages((m) => [...m, userMsg, placeholder]);
    setBusy(true);

    // Persist the user turn immediately so it survives a crash mid-stream.
    try {
      await ipc.appendMessage({
        id: userId,
        conversation_id: target,
        role: "user",
        content: prompt,
        agent_id: null,
        model: null,
        tokens: null,
        created_at: nowMs(),
      });
    } catch (e) {
      setError(`persist user turn failed: ${e}`);
    }

    let acc = "";
    try {
      await ipc.localGenerateStream(model, prompt, (chunk) => {
        acc += chunk.text;
        setMessages((m) => {
          const next = [...m];
          next[next.length - 1] = {
            ...next[next.length - 1],
            content: acc,
            pending: !chunk.done,
          };
          return next;
        });
      });
      // Persist the assistant reply once streaming completes.
      await ipc.appendMessage({
        id: assistantId,
        conversation_id: target,
        role: "assistant",
        content: acc,
        agent_id: "personal-agent",
        model: `${model} · ${backend}`,
        tokens: null,
        created_at: nowMs(),
      });
      await refreshConversations();
      // Fire-and-forget telemetry; silently a no-op when offline.
      ipc
        .pushTelemetry(deviceId || "unpaired", [
          {
            eventType: "conversation.turn",
            timestampMs: nowMs(),
            attributes: { model, backend, chars: acc.length },
          },
        ])
        .catch(() => {});
    } catch (e) {
      const msg = String(e);
      setError(msg);
      setMessages((m) => {
        const next = [...m];
        next[next.length - 1] = {
          ...next[next.length - 1],
          content: msg,
          pending: false,
        };
        return next;
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-1 min-h-0">
      <ConversationList
        items={conversations}
        activeId={activeId}
        onSelect={setActiveId}
        onNew={newConversation}
      />

      <main className="flex-1 flex flex-col min-h-0">
        <div className="px-6 py-3 border-b border-border flex items-center gap-3">
          <span className="font-medium">
            {conversations.find((c) => c.id === activeId)?.title ?? "Conversation"}
          </span>
          <span className="pill pill-standard font-mono">backend · {backend}</span>
          {activeId ? (
            <button className="btn btn-ghost ml-auto" onClick={deleteActive}>
              Delete
            </button>
          ) : null}
          <button
            className="btn btn-ghost"
            onClick={() => setShowInspector((s) => !s)}
          >
            {showInspector ? "Hide" : "Show"} inspector
          </button>
        </div>

        <div className="flex-1 overflow-auto px-6 py-4 flex flex-col gap-4">
          {messages.length === 0 ? (
            <div className="text-text-3 text-sm">
              Ask anything. Replies route through the local backend
              (<span className="font-mono">{backend}</span>) and stay on-device.
              History is persisted in your edge SQLite store.
            </div>
          ) : (
            messages.map((m) => <Bubble key={m.id} m={m} />)
          )}
          <div ref={tail} />
        </div>

        {error ? (
          <div className="px-6 py-2 text-xs text-danger border-t border-border bg-danger/10">
            {error}
          </div>
        ) : null}

        <div className="border-t border-border p-3">
          <div className="card !p-2 flex items-center gap-2">
            <input
              className="input flex-1 !bg-transparent !border-0 !rounded-none focus:!ring-0"
              placeholder="Ask Lucifer or @mention an agent…"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  void send();
                }
              }}
              disabled={busy}
            />
            <button
              className="btn btn-primary"
              onClick={() => void send()}
              disabled={busy || !input.trim()}
            >
              {busy ? "…" : "Send"}
            </button>
          </div>
        </div>
      </main>

      {showInspector ? <Inspector backend={backend} /> : null}
    </div>
  );
}

function ConversationList({
  items,
  activeId,
  onSelect,
  onNew,
}: {
  items: StoredConversation[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
}) {
  return (
    <aside className="w-60 shrink-0 border-r border-border bg-surface flex flex-col">
      <div className="px-3 py-2 border-b border-border">
        <button className="btn btn-ghost w-full text-xs" onClick={onNew}>
          + New conversation
        </button>
      </div>
      <ul className="flex-1 overflow-auto p-2 flex flex-col gap-1">
        {items.length === 0 ? (
          <li className="text-xs text-text-3 px-2 py-2">No history yet.</li>
        ) : (
          items.map((c) => (
            <li
              key={c.id}
              onClick={() => onSelect(c.id)}
              className={`px-2 py-1.5 rounded cursor-pointer text-sm truncate ${
                activeId === c.id ? "bg-surface-elev text-text" : "text-text-2 hover:bg-surface-elev"
              }`}
            >
              <div className="truncate">{c.title || "(untitled)"}</div>
              <div className="text-[10px] text-text-3">
                {c.message_count} msg · {new Date(c.updated_at).toLocaleString()}
              </div>
            </li>
          ))
        )}
      </ul>
    </aside>
  );
}

function Bubble({ m }: { m: Msg }) {
  if (m.role === "user") {
    return (
      <div className="self-end max-w-2xl card !bg-primary/15 !border-primary/30 text-text">
        {m.content}
      </div>
    );
  }
  const a = agent((m.agent ?? "personal-agent") as any);
  return (
    <div className="self-start max-w-2xl card">
      <div className="flex items-center gap-2 mb-1.5 text-xs text-text-2">
        <span className="size-2 rounded-full" style={{ background: a.color }} />
        <span className="font-mono">{a.id}</span>
        {m.model ? (
          <>
            <span>·</span>
            <span className="font-mono">{m.model}</span>
          </>
        ) : null}
      </div>
      {m.pending ? (
        <div className="text-text-3 italic">…thinking</div>
      ) : (
        <div className="whitespace-pre-wrap">{m.content}</div>
      )}
    </div>
  );
}

function Inspector({ backend }: { backend: string }) {
  return (
    <aside className="w-80 shrink-0 border-l border-border bg-surface px-4 py-4 overflow-auto">
      <div className="text-[10px] tracking-widest text-text-3 font-semibold uppercase mb-2">
        Context Package
      </div>

      <div className="card !p-2.5 mb-3 text-xs">
        <div className="font-medium mb-1">Local backend</div>
        <span className="pill pill-standard font-mono">{backend}</span>
      </div>

      <div className="text-xs text-text-2 mb-1.5">ACL — readable</div>
      <ul className="text-[11px] font-mono text-text-3 space-y-0.5">
        <li>Person · read+write</li>
        <li>Financial · read</li>
        <li>HealthRecord · read</li>
      </ul>

      <div className="mt-3 text-[10px] text-text-3 leading-relaxed">
        History is persisted to <span className="font-mono">edge_store.db</span> in
        your app data directory. Delete a conversation to remove it permanently.
      </div>
    </aside>
  );
}
