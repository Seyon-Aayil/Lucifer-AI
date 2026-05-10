import { useEffect, useRef, useState } from "react";
import { agent } from "../lib/agents";
import { ipc } from "../lib/ipc";

type Msg = {
  role: "user" | "assistant";
  content: string;
  agent?: string;
  model?: string;
  pending?: boolean;
};

const DEFAULT_MODEL = "llama3.2";

export function Conversation() {
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [backend, setBackend] = useState("ollama");
  const [error, setError] = useState<string | null>(null);
  const [showInspector, setShowInspector] = useState(true);
  const tail = useRef<HTMLDivElement>(null);

  useEffect(() => {
    ipc.localBackend().then(setBackend).catch(() => {});
  }, []);

  useEffect(() => {
    tail.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function send() {
    const prompt = input.trim();
    if (!prompt || busy) return;
    setError(null);
    setInput("");
    const userMsg: Msg = { role: "user", content: prompt };
    const placeholder: Msg = {
      role: "assistant",
      content: "",
      agent: "personal-agent",
      model: `${DEFAULT_MODEL} · ${backend}`,
      pending: true,
    };
    setMessages((m) => [...m, userMsg, placeholder]);
    setBusy(true);
    try {
      let acc = "";
      await ipc.localGenerateStream(DEFAULT_MODEL, prompt, (chunk) => {
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
      <main className="flex-1 flex flex-col min-h-0">
        <div className="px-6 py-3 border-b border-border flex items-center gap-3">
          <span className="font-medium">Conversation</span>
          <span className="pill pill-standard font-mono">backend · {backend}</span>
          <button
            className="btn btn-ghost ml-auto"
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
            </div>
          ) : (
            messages.map((m, i) => <Bubble key={i} m={m} />)
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
        <span>·</span>
        <span className="font-mono">{m.model}</span>
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
    </aside>
  );
}
