import { useState } from "react";

const PENDING = [
  { id: "1", agent: "personal-agent",  color: "#7C5CFF", action: "Send digest email",  risk: "high",       undo: 0.7 },
  { id: "2", agent: "financial-agent", color: "#4ADE80", action: "Categorise expenses", risk: "medium",    undo: 0.2 },
  { id: "3", agent: "coding-agent",    color: "#22D3EE", action: "Open PR on repo X",   risk: "irreversible", undo: 0.95 },
];

export function Approvals() {
  const [selected, setSelected] = useState(PENDING[0].id);
  const sel = PENDING.find((p) => p.id === selected)!;

  return (
    <div className="flex-1 flex min-h-0">
      <aside className="w-80 shrink-0 border-r border-border bg-surface flex flex-col">
        <div className="flex gap-1 px-3 py-2 border-b border-border text-xs">
          {["all", "low", "medium", "high", "irrev."].map((t) => (
            <button key={t} className="btn btn-ghost !py-1 !px-2 text-[11px]">{t}</button>
          ))}
        </div>
        <ul className="flex-1 overflow-auto p-2 flex flex-col gap-1.5">
          {PENDING.map((p) => (
            <li
              key={p.id}
              onClick={() => setSelected(p.id)}
              className={`card !p-2.5 cursor-pointer ${selected === p.id ? "!border-primary/60" : ""}`}
            >
              <div className="flex items-center gap-2 text-xs">
                <span className="size-2 rounded-full" style={{ background: p.color }} />
                <span className="font-mono">{p.agent}</span>
                <span className="ml-auto pill pill-restricted">{p.risk}</span>
              </div>
              <div className="text-sm mt-1">{p.action}</div>
              <div className="mt-2 h-1 rounded bg-surface-elev overflow-hidden">
                <div className="h-full bg-warn" style={{ width: `${p.undo * 100}%` }} />
              </div>
              <div className="text-[10px] text-text-3 mt-1">undo difficulty</div>
            </li>
          ))}
        </ul>
      </aside>

      <main className="flex-1 overflow-auto px-6 py-6">
        <div className="text-[10px] tracking-widest font-semibold text-text-3 uppercase mb-2">
          Approve
        </div>
        <h1 className="text-2xl font-semibold mb-4">{sel.action}</h1>

        <div className="card mb-4">
          <div className="text-xs text-text-2 mb-1">What this does</div>
          <p className="text-sm">
            Lorem ipsum: this action is proposed by{" "}
            <span className="font-mono">{sel.agent}</span> and will execute the listed tool call against an external service.
          </p>
        </div>

        <div className="card mb-4">
          <div className="text-xs text-text-2 mb-1">Tool call</div>
          <pre className="font-mono text-xs">
{`{
  "tool": "gmail.send_message",
  "args": { "to": ["…"], "subject": "Weekly digest", "body": "…" }
}`}
          </pre>
        </div>

        <div className="card mb-4">
          <div className="text-xs text-text-2 mb-1">Predicted side effects</div>
          <ul className="text-sm list-disc pl-5">
            <li>Sends a message to N recipients</li>
            <li>Logs an outbound event in telemetry</li>
            <li>Updates Person.lastContacted edges</li>
          </ul>
        </div>

        <div className="card mb-6">
          <div className="text-xs text-text-2 mb-1">Undo difficulty</div>
          <div className="h-2 rounded bg-surface-elev overflow-hidden">
            <div className="h-full bg-danger" style={{ width: `${sel.undo * 100}%` }} />
          </div>
          <div className="text-xs text-text-2 mt-1">Hard — recipient cannot un-receive.</div>
        </div>

        <div className="flex gap-2 mb-3">
          <button className="btn btn-primary">Approve (5s)</button>
          <button className="btn btn-ghost">Approve with edits</button>
          <button className="btn btn-danger">Reject</button>
        </div>
        <label className="flex items-center gap-2 text-xs text-text-2">
          <input type="checkbox" /> Always allow this tool from {sel.agent}
        </label>
      </main>
    </div>
  );
}
