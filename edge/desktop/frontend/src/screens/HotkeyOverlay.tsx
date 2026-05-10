import { useState } from "react";

export function HotkeyOverlay() {
  const [streaming, setStreaming] = useState(false);

  return (
    <div className="flex-1 flex items-start justify-center pt-16 px-6 bg-bg">
      <div className="w-[720px] card !p-0 !rounded-lg overflow-hidden backdrop-blur"
           style={{ boxShadow: "0 24px 60px rgba(0,0,0,0.6), inset 0 0 0 1px rgba(255,255,255,0.06)" }}>
        <div className="flex items-center gap-3 px-4 py-2.5 border-b border-border bg-surface-elev">
          <span className="size-2 rounded-full bg-primary shadow-[0_0_10px_#7C5CFF]" />
          <div className="pill bg-primary/15 text-primary">
            personal-agent
            <span className="text-text-3 ml-2">picked by intent classifier</span>
          </div>
          <div className="ml-auto flex items-center gap-2 text-[11px] font-mono text-text-2">
            <span>1.2k / 8k tok</span>
            <span className="size-1 rounded-full bg-text-3" />
            <span className="pill pill-standard !text-success !border-success/40">local</span>
          </div>
        </div>

        <div className="p-4">
          <input
            autoFocus
            className="input w-full !bg-transparent !border-0 !text-base font-mono"
            placeholder="Ask or describe a task…"
          />
        </div>

        <div className="px-4 pb-4 max-h-[420px] overflow-auto flex flex-col gap-3">
          <div className="card text-sm">
            <div className="text-text-2">
              {streaming ? "streaming…" : "Lorem ipsum dolor sit amet, the assistant streams tokens here."}
            </div>
            <pre className="font-mono text-xs mt-3 bg-bg/40 border border-border rounded p-2">
{"def hello(name: str) -> str:\n    return f\"hi {name}\""}
            </pre>
          </div>

          <div
            className="card !p-3 !border-warn/60"
            style={{ animation: "pulse 2s ease-in-out infinite" }}
          >
            <div className="flex items-center gap-2 text-warn font-medium text-sm">
              <span>HitL approval needed</span>
              <span className="ml-auto text-xs text-text-2">5s wait</span>
            </div>
            <div className="text-xs text-text-2 mt-1.5">
              Schedule reminder for tomorrow 9am — preview shown below.
            </div>
            <div className="flex gap-2 mt-3">
              <button className="btn btn-primary">Approve</button>
              <button className="btn btn-ghost">Approve with edits</button>
              <button className="btn btn-danger">Reject</button>
            </div>
          </div>
        </div>

        <div className="px-4 py-2 border-t border-border bg-surface-elev text-[11px] text-text-3 flex gap-4">
          <span>Esc close</span>
          <span>↩ submit</span>
          <span>⌘K refocus</span>
          <button
            className="ml-auto text-text-2 hover:text-text"
            onClick={() => setStreaming((s) => !s)}
          >
            {streaming ? "stop demo" : "start demo"}
          </button>
        </div>
      </div>
    </div>
  );
}
