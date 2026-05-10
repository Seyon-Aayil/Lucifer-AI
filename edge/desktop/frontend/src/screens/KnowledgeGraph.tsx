import { useEffect, useState } from "react";
import { ipc, EdgeNode } from "../lib/ipc";

const NODE_TYPES = [
  { label: "Person", color: "#7C5CFF" },
  { label: "Place", color: "#22D3EE" },
  { label: "Event", color: "#4ADE80" },
  { label: "Concept", color: "#F472B6" },
  { label: "Artifact", color: "#FBBF24" },
  { label: "News", color: "#60A5FA" },
];

function decodePayload(bytes: number[]): unknown {
  try {
    const text = new TextDecoder().decode(new Uint8Array(bytes));
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function classificationPill(c: string): string {
  return c === "secret"
    ? "pill-secret"
    : c === "restricted"
    ? "pill-restricted"
    : c === "public"
    ? "pill-public"
    : "pill-standard";
}

export function KnowledgeGraph() {
  const [type, setType] = useState("Person");
  const [nodes, setNodes] = useState<EdgeNode[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function refresh(t: string) {
    setError(null);
    try {
      const rows = await ipc.listNodesByType(t, 50);
      setNodes(rows);
      setSelected(rows[0]?.node_id ?? null);
    } catch (e) {
      setError(String(e));
    }
  }

  useEffect(() => {
    refresh(type);
  }, [type]);

  const sel = nodes.find((n) => n.node_id === selected);
  const payload = sel ? decodePayload(sel.payload) : null;

  return (
    <div className="flex-1 flex flex-col min-h-0">
      <div className="flex items-center gap-2 px-4 py-2 border-b border-border bg-surface flex-wrap">
        <span className="text-xs text-text-2 mr-2">Type</span>
        {NODE_TYPES.map((t) => (
          <button
            key={t.label}
            onClick={() => setType(t.label)}
            className={`pill text-xs font-mono ${type === t.label ? "" : "opacity-60"}`}
            style={{
              background: `${t.color}20`,
              color: t.color,
              border: `1px solid ${t.color}55`,
            }}
          >
            {t.label}
          </button>
        ))}
        <span className="ml-auto text-[11px] text-text-3 font-mono">{nodes.length} rows</span>
      </div>

      <div className="flex-1 grid grid-cols-[2fr_1fr] gap-px bg-border min-h-0">
        <div className="bg-bg overflow-auto">
          {nodes.length === 0 ? (
            <div className="p-6 text-text-3 text-sm">
              No <span className="font-mono">{type}</span> nodes in the local mirror yet. Pull
              the hot subgraph from the connection bar to seed it.
            </div>
          ) : (
            <table className="w-full text-xs">
              <thead className="text-text-3 text-left">
                <tr>
                  <th className="px-3 py-2">id</th>
                  <th className="px-3 py-2">classification</th>
                  <th className="px-3 py-2">source</th>
                  <th className="px-3 py-2">updated</th>
                </tr>
              </thead>
              <tbody>
                {nodes.map((n) => (
                  <tr
                    key={n.node_id}
                    className={`border-t border-border cursor-pointer hover:bg-surface ${
                      selected === n.node_id ? "bg-surface" : ""
                    }`}
                    onClick={() => setSelected(n.node_id)}
                  >
                    <td className="px-3 py-1.5 font-mono text-text">{n.node_id}</td>
                    <td className="px-3 py-1.5">
                      <span className={`pill ${classificationPill(n.classification)}`}>
                        {n.classification}
                      </span>
                    </td>
                    <td className="px-3 py-1.5 font-mono text-text-2">{n.source_agent}</td>
                    <td className="px-3 py-1.5 font-mono text-text-3">
                      {new Date(n.updated_at).toLocaleString()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div className="bg-surface px-4 py-4 overflow-auto">
          {sel ? (
            <>
              <div className="flex items-center gap-2 mb-2">
                <span className="font-mono">{sel.node_id}</span>
                <span className={`pill ${classificationPill(sel.classification)} ml-auto`}>
                  {sel.classification}
                </span>
              </div>
              <div className="text-xs text-text-2 mb-1">Payload</div>
              <pre className="font-mono text-[11px] bg-surface-elev rounded p-2 mb-3 overflow-auto">
                {payload ? JSON.stringify(payload, null, 2) : "(could not decode)"}
              </pre>
              <div className="text-xs text-text-2 mb-1">Updated</div>
              <div className="font-mono text-[11px] mb-3">
                {new Date(sel.updated_at).toISOString()}
              </div>
              <div className="text-xs text-text-2 mb-1">Source agent</div>
              <div className="font-mono text-[11px] mb-3">{sel.source_agent || "—"}</div>
            </>
          ) : (
            <div className="text-text-3 text-sm">Select a node to see its payload.</div>
          )}
        </div>
      </div>

      {error ? (
        <div className="px-4 py-2 text-[11px] text-danger border-t border-border bg-danger/10">
          {error}
        </div>
      ) : null}
    </div>
  );
}
