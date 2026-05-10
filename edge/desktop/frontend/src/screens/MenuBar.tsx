import { AGENTS } from "../lib/agents";
import { useStats } from "../lib/useStats";

function fmtTs(ms: number | null): string {
  if (!ms) return "never";
  const dt = new Date(ms);
  return dt.toLocaleTimeString();
}

export function MenuBar() {
  const stats = useStats();
  const totalActions =
    (stats.queue?.pending ?? 0) +
    (stats.queue?.in_flight ?? 0) +
    (stats.queue?.completed ?? 0) +
    (stats.queue?.failed ?? 0);

  return (
    <div className="flex-1 flex items-start justify-center pt-12 px-6">
      <div className="w-[360px] card !p-0 !rounded-lg overflow-hidden">
        <div className="flex items-center gap-2 px-4 py-3 border-b border-border bg-surface-elev">
          <span
            className={`size-2 rounded-full ${
              stats.connected
                ? "bg-success shadow-[0_0_10px_#4ADE80]"
                : "bg-text-3"
            }`}
          />
          <span className="text-xs">
            {stats.connected
              ? `Connected · synced ${fmtTs(stats.store?.last_sync_at_ms ?? null)}`
              : "Offline"}
          </span>
          <span className="ml-auto text-[11px] text-text-3 font-mono">mTLS</span>
        </div>

        <div className="grid grid-cols-2 gap-px bg-border">
          {[
            { k: "KG nodes", v: stats.store?.node_count ?? 0 },
            { k: "KG edges", v: stats.store?.edge_count ?? 0 },
            { k: "Queue",    v: stats.queue?.pending ?? 0 },
            { k: "In-flight", v: stats.queue?.in_flight ?? 0 },
          ].map((cell) => (
            <div key={cell.k} className="bg-surface px-3 py-2.5 text-xs">
              <div className="text-text-3">{cell.k}</div>
              <div className="font-mono text-text">{cell.v.toLocaleString()}</div>
            </div>
          ))}
        </div>

        <div className="px-4 py-3 border-t border-border">
          <div className="flex items-center gap-2 mb-2">
            <span className="text-xs font-medium">Action queue</span>
            <span className="ml-auto pill pill-restricted">
              {stats.queue?.pending ?? 0} pending
            </span>
          </div>
          <div className="text-xs text-text-2 leading-relaxed">
            <div>Completed: <span className="font-mono text-text">{stats.queue?.completed ?? 0}</span></div>
            <div>Failed:    <span className="font-mono text-danger">{stats.queue?.failed ?? 0}</span></div>
            <div>Total:     <span className="font-mono text-text">{totalActions.toLocaleString()}</span></div>
          </div>
        </div>

        <div className="px-4 py-3 border-t border-border">
          <div className="text-xs font-medium mb-2">Agents</div>
          <ul className="flex flex-col gap-1.5 text-xs text-text-2">
            {AGENTS.slice(0, 5).map((a) => (
              <li key={a.id} className="flex items-center gap-2">
                <span className="size-2 rounded-full" style={{ background: a.color }} />
                <span className="font-mono text-text">{a.id}</span>
                <span className="ml-auto text-text-3">{a.blurb}</span>
              </li>
            ))}
          </ul>
        </div>

        <div className="px-4 py-2.5 border-t border-border bg-surface-elev flex items-center gap-3 text-[11px] text-text-3">
          <span className="font-mono">⌘+Space</span>
          <span className="ml-auto cursor-pointer hover:text-text">Settings</span>
          <span className="cursor-pointer hover:text-text">Quit</span>
        </div>
      </div>
    </div>
  );
}
