const NODE_TYPES = [
  { label: "Person", color: "#7C5CFF" },
  { label: "Place", color: "#22D3EE" },
  { label: "Event", color: "#4ADE80" },
  { label: "Concept", color: "#F472B6" },
  { label: "Artifact", color: "#FBBF24" },
  { label: "News", color: "#60A5FA" },
];

export function KnowledgeGraph() {
  return (
    <div className="flex-1 flex flex-col min-h-0">
      <div className="flex items-center gap-2 px-4 py-2 border-b border-border bg-surface flex-wrap">
        <input className="input flex-1 min-w-[240px]" placeholder="semantic + label search…" />
        <div className="flex items-center gap-1">
          <button className="btn btn-ghost !py-1 !px-2.5 text-xs">Graph</button>
          <button className="btn btn-ghost !py-1 !px-2.5 text-xs">Table</button>
        </div>
      </div>

      <div className="flex items-center gap-1.5 px-4 py-2 border-b border-border overflow-x-auto bg-surface">
        {NODE_TYPES.map((t) => (
          <span
            key={t.label}
            className="pill text-xs font-mono"
            style={{
              background: `${t.color}20`,
              color: t.color,
              border: `1px solid ${t.color}55`,
            }}
          >
            {t.label}
          </span>
        ))}
        <div className="w-px h-4 bg-border mx-2" />
        <span className="pill pill-public">public</span>
        <span className="pill pill-standard">standard</span>
        <span className="pill pill-restricted">restricted ⌫</span>
        <span className="pill pill-secret">secret 🔒</span>
      </div>

      <div className="flex-1 grid grid-cols-[2fr_1fr] gap-px bg-border min-h-0">
        <div className="bg-bg flex items-center justify-center text-text-3 text-sm">
          <svg width="100%" height="100%" viewBox="0 0 600 400" className="max-w-full max-h-full">
            {[
              { x: 200, y: 200, c: "#7C5CFF" },
              { x: 320, y: 140, c: "#22D3EE" },
              { x: 440, y: 220, c: "#4ADE80" },
              { x: 380, y: 320, c: "#F472B6" },
              { x: 160, y: 100, c: "#FBBF24" },
              { x: 120, y: 300, c: "#F87171" },
            ].map((n, i) => (
              <g key={i}>
                <circle cx={n.x} cy={n.y} r={n.c === "#F87171" ? 24 : 18}
                        fill={n.c} fillOpacity={n.c === "#F87171" ? 0.25 : 0.18}
                        stroke={n.c} strokeWidth={n.c === "#F87171" ? 2 : 1} />
                {n.c === "#F87171" ? (
                  <circle cx={n.x} cy={n.y} r={32} fill="none" stroke={n.c} strokeWidth={1} strokeDasharray="3 3" opacity={0.6}/>
                ) : null}
              </g>
            ))}
          </svg>
        </div>

        <div className="bg-surface px-4 py-4 overflow-auto">
          <div className="flex items-center gap-2 mb-2">
            <span className="font-medium">Alex Chen</span>
            <span className="pill pill-restricted ml-auto">restricted 🔒</span>
          </div>
          <div className="text-[11px] text-text-3 mb-3">never sent to cloud LLMs</div>

          <div className="text-xs text-text-2 mb-1">Attributes</div>
          <pre className="font-mono text-[11px] bg-surface-elev rounded p-2 mb-3">
{`{
  "name": "Alex Chen",
  "role": "Lead Architect",
  "team": "Core Systems"
}`}
          </pre>

          <div className="text-xs text-text-2 mb-1">Edges</div>
          <ul className="text-[11px] text-text-3 font-mono mb-3 space-y-0.5">
            <li>WORKS_WITH → Person:Sam (0.84)</li>
            <li>AUTHORED → Artifact:RFC-014 (1.0)</li>
            <li>MEMBER_OF → Concept:Platform (0.7)</li>
          </ul>

          <div className="text-xs text-text-2 mb-1">Decay history</div>
          <svg viewBox="0 0 100 24" className="w-full h-8 mb-4">
            <polyline
              fill="none"
              stroke="#7C5CFF"
              strokeWidth="1.5"
              points="0,4 14,5 28,7 42,10 56,13 70,17 84,20 98,22"
            />
          </svg>

          <div className="flex gap-2">
            <button className="btn btn-danger">Soft delete</button>
            <button className="btn btn-primary">Promote</button>
          </div>
        </div>
      </div>

      <div className="px-4 py-2 text-[11px] text-text-3 border-t border-border bg-surface">
        Showing 312 of 1,847 nodes · 64 ACL-filtered
      </div>
    </div>
  );
}
