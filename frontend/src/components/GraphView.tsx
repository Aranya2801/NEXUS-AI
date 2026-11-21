import type { GraphQuery } from "../types/schemas";

const COLORS = ["#0f766e", "#6366f1", "#b45309", "#be185d"];

/** Deterministic radial layout: seed entities in the centre, each further hop on a larger ring. No physics, so it is stable and testable. */
export function layout(nodes: GraphQuery["nodes"], cx = 320, cy = 230): Record<string, { x: number; y: number }> {
  const byDepth: Record<number, string[]> = {};
  for (const id of Object.keys(nodes).sort()) (byDepth[nodes[id]!.depth] ??= []).push(id);
  const pos: Record<string, { x: number; y: number }> = {};
  for (const [d, ids] of Object.entries(byDepth)) {
    const depth = Number(d), r = depth === 0 ? (ids.length > 1 ? 40 : 0) : 70 + depth * 55;
    ids.forEach((id, i) => {
      const a = (2 * Math.PI * i) / ids.length - Math.PI / 2 + depth * 0.4;
      pos[id] = { x: cx + r * Math.cos(a), y: cy + r * Math.sin(a) };
    });
  }
  return pos;
}

export function GraphView({ nodes, edges, onSelect }: { nodes: GraphQuery["nodes"]; edges: GraphQuery["edges"]; onSelect?: (id: string) => void }) {
  const pos = layout(nodes);
  return (
    <svg viewBox="0 0 640 460" role="img" aria-label="Knowledge graph" style={{ width: "100%", maxHeight: 460 }}>
      {edges.map((e, i) => {
        const a = pos[e.source_id], b = pos[e.target_id];
        if (!a || !b) return null;
        return (
          <g key={i} data-edge={e.predicate}>
            <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke="currentColor" opacity={0.3 + 0.5 * e.confidence} strokeWidth={1.2} />
            <text x={(a.x + b.x) / 2} y={(a.y + b.y) / 2 - 3} fontSize="9" textAnchor="middle" opacity="0.75">{e.predicate}</text>
          </g>
        );
      })}
      {Object.values(nodes).map((n) => {
        const p = pos[n.id];
        if (!p) return null;
        return (
          <g key={n.id} data-node={n.name ?? n.id} style={{ cursor: "pointer" }} onClick={() => onSelect?.(n.id)}>
            <circle cx={p.x} cy={p.y} r={n.depth === 0 ? 11 : 8} fill={COLORS[Math.min(n.depth, 3)]} />
            <text x={p.x} y={p.y + (n.depth === 0 ? 24 : 20)} fontSize="11" textAnchor="middle">{n.name ?? "…"}</text>
          </g>
        );
      })}
    </svg>
  );
}
