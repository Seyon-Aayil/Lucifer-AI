import { NavLink } from "react-router-dom";
import { AGENTS } from "../lib/agents";

const NAV: { to: string; label: string }[] = [
  { to: "/conversation",  label: "Conversation" },
  { to: "/graph",         label: "Knowledge Graph" },
  { to: "/approvals",     label: "Approvals" },
  { to: "/menu-bar",      label: "Status" },
  { to: "/overlay",       label: "Hotkey Overlay" },
  { to: "/onboarding",    label: "Onboarding" },
  { to: "/settings",      label: "Settings" },
];

export function Sidebar() {
  return (
    <aside className="w-60 shrink-0 border-r border-border bg-surface flex flex-col">
      <div className="px-4 py-4 flex items-center gap-2">
        <span className="inline-block size-2 rounded-full bg-primary shadow-[0_0_12px_#7C5CFF]" />
        <span className="font-semibold tracking-tight">Lucifer</span>
      </div>

      <nav className="px-2 flex flex-col gap-0.5">
        {NAV.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            className={({ isActive }) =>
              `px-2 py-1.5 rounded text-sm ${
                isActive ? "bg-surface-elev text-text" : "text-text-2 hover:bg-surface-elev"
              }`
            }
          >
            {item.label}
          </NavLink>
        ))}
      </nav>

      <div className="mt-6 px-2">
        <div className="px-2 mb-1 text-[10px] tracking-widest font-semibold text-text-3 uppercase">
          Agents
        </div>
        <ul className="flex flex-col gap-0.5">
          {AGENTS.map((a) => (
            <li
              key={a.id}
              className="flex items-center gap-2 px-2 py-1 text-xs text-text-2"
            >
              <span
                className="inline-block size-2 rounded-full"
                style={{ background: a.color }}
              />
              <span className="font-mono">{a.id}</span>
            </li>
          ))}
        </ul>
      </div>

      <div className="mt-auto px-3 py-3 text-[11px] text-text-3 border-t border-border">
        v0.1.0 · Phase 4b
      </div>
    </aside>
  );
}
