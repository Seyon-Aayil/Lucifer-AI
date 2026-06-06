import { useEffect, useState } from "react";
import { ipc, type AgentResult, PendingAction } from "../lib/ipc";

const COLOR: Record<string, string> = {
  "personal-agent":  "#7C5CFF",
  "coding-agent":    "#22D3EE",
  "financial-agent": "#4ADE80",
  "health-agent":    "#F472B6",
  "research-agent":  "#FBBF24",
};

function colorFor(actionType: string): string {
  // action_type prefix carries the agent id by convention
  const a = Object.keys(COLOR).find((k) => actionType.startsWith(k));
  return a ? COLOR[a] : "#9CA0AB";
}

export function Approvals() {
  const [actions, setActions] = useState<PendingAction[]>([]);
  const [results, setResults] = useState<AgentResult[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function refresh() {
    try {
      const list = await ipc.listPendingActions(50);
      setActions(list);
      if (list.length && !selected) setSelected(list[0].id);
    } catch (e) {
      setError(String(e));
    }
    // Pull any agent results the master finished for this device (best-effort;
    // no-op when offline). Newest first, accumulated across polls.
    try {
      const settings = await ipc.getSettings();
      if (settings.device_id) {
        const fresh = await ipc.getPendingResults(settings.device_id);
        if (fresh.length) {
          setResults((prev) => [...fresh, ...prev].slice(0, 50));
        }
      }
    } catch {
      /* offline / not connected — ignore */
    }
  }

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 5000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function seedDemo() {
    setBusy(true);
    try {
      await ipc.enqueueOfflineAction(
        "personal-agent.send_email",
        JSON.stringify({ to: "ops@example.org", subject: "Weekly digest" }),
      );
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  const sel = actions.find((a) => a.id === selected);

  async function approve() {
    if (!sel) return;
    setBusy(true);
    try {
      await ipc.markActionCompleted(sel.id);
      setSelected(null);
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function reject() {
    if (!sel) return;
    setBusy(true);
    try {
      await ipc.markActionFailed(sel.id, "rejected by user");
      setSelected(null);
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex-1 flex min-h-0">
      <aside className="w-80 shrink-0 border-r border-border bg-surface flex flex-col">
        <div className="flex items-center gap-2 px-3 py-2 border-b border-border text-xs">
          <span className="text-text-2">Pending</span>
          <span className="pill pill-restricted ml-auto">{actions.length}</span>
        </div>
        <div className="px-3 py-2 border-b border-border">
          <button className="btn btn-ghost text-xs w-full" onClick={seedDemo} disabled={busy}>
            Seed demo action
          </button>
        </div>
        <ul className="flex-1 overflow-auto p-2 flex flex-col gap-1.5">
          {actions.length === 0 ? (
            <li className="text-xs text-text-3 px-1">No queued actions.</li>
          ) : (
            actions.map((a) => (
              <li
                key={a.id}
                onClick={() => setSelected(a.id)}
                className={`card !p-2.5 cursor-pointer ${
                  selected === a.id ? "!border-primary/60" : ""
                }`}
              >
                <div className="flex items-center gap-2 text-xs">
                  <span className="size-2 rounded-full" style={{ background: colorFor(a.action_type) }} />
                  <span className="font-mono truncate">{a.action_type}</span>
                  <span className="ml-auto pill pill-standard">attempt {a.attempt_count}</span>
                </div>
                <div className="text-[10px] text-text-3 mt-1">id {a.id.slice(0, 8)}…</div>
              </li>
            ))
          )}
        </ul>

        <div className="border-t border-border">
          <div className="flex items-center gap-2 px-3 py-2 text-xs">
            <span className="text-text-2">Completed</span>
            <span className="pill pill-standard ml-auto">{results.length}</span>
          </div>
          <ul className="max-h-56 overflow-auto p-2 flex flex-col gap-1.5">
            {results.length === 0 ? (
              <li className="text-xs text-text-3 px-1">No agent results yet.</li>
            ) : (
              results.map((r) => (
                <li key={r.task_id} className="card !p-2.5">
                  <div className="flex items-center gap-2 text-xs">
                    <span
                      className="size-2 rounded-full"
                      style={{ background: colorFor(r.agent_id) }}
                    />
                    <span className="font-mono truncate">{r.agent_id}</span>
                  </div>
                  <div className="text-[11px] text-text-2 mt-1 line-clamp-3">
                    {r.final_output || "(no output)"}
                  </div>
                </li>
              ))
            )}
          </ul>
        </div>
      </aside>

      <main className="flex-1 overflow-auto px-6 py-6">
        {sel ? (
          <>
            <div className="text-[10px] tracking-widest font-semibold text-text-3 uppercase mb-2">
              Approve
            </div>
            <h1 className="text-2xl font-semibold mb-4 font-mono">{sel.action_type}</h1>

            <div className="card mb-4 text-xs">
              <div className="text-text-2 mb-1">Action id</div>
              <div className="font-mono">{sel.id}</div>
            </div>

            <div className="card mb-4 text-xs">
              <div className="text-text-2 mb-1">Status</div>
              <span className="pill pill-restricted">{sel.status}</span>
              <span className="ml-3 text-text-3">attempt {sel.attempt_count}</span>
            </div>

            {sel.last_error ? (
              <div className="card mb-4 text-xs">
                <div className="text-text-2 mb-1">Last error</div>
                <pre className="font-mono">{sel.last_error}</pre>
              </div>
            ) : null}

            <div className="flex gap-2 mb-3">
              <button className="btn btn-primary" disabled={busy} onClick={approve}>
                Approve
              </button>
              <button className="btn btn-danger" disabled={busy} onClick={reject}>
                Reject
              </button>
            </div>
          </>
        ) : (
          <div className="text-text-3 text-sm">
            No action selected. Use "Seed demo action" or wait for the offline queue to receive one.
          </div>
        )}

        {error ? <div className="text-sm text-danger mt-3">{error}</div> : null}
      </main>
    </div>
  );
}
