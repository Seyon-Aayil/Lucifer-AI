import { AGENTS } from "../lib/agents";

export function MenuBar() {
  return (
    <div className="flex-1 flex items-start justify-center pt-12 px-6">
      <div className="w-[360px] card !p-0 !rounded-lg overflow-hidden">
        <div className="flex items-center gap-2 px-4 py-3 border-b border-border bg-surface-elev">
          <span className="size-2 rounded-full bg-success shadow-[0_0_10px_#4ADE80]" />
          <span className="text-xs">Connected · last sync just now</span>
          <span className="ml-auto text-[11px] text-text-3 font-mono">mTLS ✓</span>
        </div>

        <div className="grid grid-cols-2 gap-px bg-border">
          {[
            { k: "Spend",    v: "$0.24 / $5.00" },
            { k: "Requests", v: "47" },
            { k: "Local",    v: "78% / 22%" },
            { k: "Avg",      v: "1.4s" },
          ].map((cell) => (
            <div key={cell.k} className="bg-surface px-3 py-2.5 text-xs">
              <div className="text-text-3">{cell.k}</div>
              <div className="font-mono text-text">{cell.v}</div>
            </div>
          ))}
        </div>

        <div className="px-4 py-3 border-t border-border">
          <div className="flex items-center gap-2 mb-2">
            <span className="text-xs font-medium">Pending approvals</span>
            <span className="ml-auto pill pill-restricted">3</span>
          </div>
          <ul className="flex flex-col gap-1.5">
            {AGENTS.slice(0, 3).map((a) => (
              <li key={a.id} className="flex items-center gap-2 text-xs">
                <span className="size-2 rounded-full" style={{ background: a.color }} />
                <span className="truncate">Send weekly digest…</span>
                <button className="ml-auto btn btn-ghost !py-0.5 !px-2 !text-[11px]">approve</button>
              </li>
            ))}
          </ul>
        </div>

        <div className="px-4 py-3 border-t border-border">
          <div className="text-xs font-medium mb-2">Recent activity</div>
          <ul className="flex flex-col gap-1.5 text-xs text-text-2">
            {AGENTS.slice(0, 5).map((a, i) => (
              <li key={a.id} className="flex items-center gap-2">
                <span className="size-2 rounded-full" style={{ background: a.color }} />
                <span className="font-mono text-text">{a.id}</span>
                <span className="truncate">summarised inbox</span>
                <span className="ml-auto text-text-3">{i + 1}m</span>
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
