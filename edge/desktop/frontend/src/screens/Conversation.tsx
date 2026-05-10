import { useState } from "react";
import { agent } from "../lib/agents";

type Msg = { role: "user" | "assistant"; content: string; agent?: string; model?: string };

const DEMO: Msg[] = [
  { role: "user", content: "What did the financial-agent flag this week?" },
  {
    role: "assistant",
    agent: "personal-agent",
    model: "claude-sonnet-4-6 · cloud",
    content:
      "Financial-agent flagged 3 transactions above your $200 threshold. Two are subscription renewals, one is a one-off purchase. Shall I categorise them for the budget review?",
  },
];

export function Conversation() {
  const [messages] = useState<Msg[]>(DEMO);
  const [showInspector, setShowInspector] = useState(true);

  return (
    <div className="flex flex-1 min-h-0">
      <main className="flex-1 flex flex-col min-h-0">
        <div className="px-6 py-3 border-b border-border flex items-center gap-3">
          <span className="font-medium">Today · Budget review</span>
          <span className="pill pill-standard">restricted</span>
          <button
            className="btn btn-ghost ml-auto"
            onClick={() => setShowInspector((s) => !s)}
          >
            {showInspector ? "Hide" : "Show"} inspector
          </button>
        </div>

        <div className="flex-1 overflow-auto px-6 py-4 flex flex-col gap-4">
          {messages.map((m, i) => (
            <Bubble key={i} m={m} />
          ))}
        </div>

        <div className="border-t border-border p-3">
          <div className="card !p-2 flex items-center gap-2">
            <input
              className="input flex-1 !bg-transparent !border-0 !rounded-none focus:!ring-0"
              placeholder="Ask Lucifer or @mention an agent…"
            />
            <button className="btn btn-primary">Send</button>
          </div>
        </div>
      </main>

      {showInspector ? <Inspector /> : null}
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
      <div>{m.content}</div>
    </div>
  );
}

function Inspector() {
  return (
    <aside className="w-80 shrink-0 border-l border-border bg-surface px-4 py-4 overflow-auto">
      <div className="text-[10px] tracking-widest text-text-3 font-semibold uppercase mb-2">
        Context Package
      </div>

      <div className="text-xs font-medium text-text-2 mb-1.5">Memories</div>
      <div className="flex flex-col gap-2 mb-4">
        <Memory text="Monthly subscriptions: Netflix, Spotify, Notion." level="standard" />
        <Memory text="Salary deposit pattern: every 30 days." level="restricted" />
        <Memory text="Bank account number: ••••" level="secret" />
      </div>

      <div className="text-xs font-medium text-text-2 mb-1.5">Graph nodes</div>
      <div className="flex flex-wrap gap-1.5 mb-4">
        {["Financial:Subscription", "Person:User", "Concept:Budget"].map((n) => (
          <span key={n} className="font-mono text-[11px] pill pill-standard">{n}</span>
        ))}
      </div>

      <div className="text-xs font-medium text-text-2 mb-1.5">ACL — readable by personal-agent</div>
      <ul className="text-[11px] font-mono text-text-3 space-y-0.5">
        <li>Person · read+write</li>
        <li>Financial · read</li>
        <li>HealthRecord · read</li>
      </ul>
    </aside>
  );
}

function Memory({ text, level }: { text: string; level: "standard" | "restricted" | "secret" }) {
  const cls = level === "secret" ? "pill-secret" : level === "restricted" ? "pill-restricted" : "pill-standard";
  return (
    <div className="card !p-2.5 text-xs">
      <span className={`pill ${cls} mr-2`}>{level}</span>
      <span className="text-text-2">{text}</span>
    </div>
  );
}
