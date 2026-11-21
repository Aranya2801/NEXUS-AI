import type { TraceEntry } from "../types/schemas";

interface N { id: string; x: number; y: number; label: string }
const W = 132, H = 34;
const NODES: N[] = [
  { id: "classify", x: 20, y: 14, label: "classify" }, { id: "research", x: 190, y: 14, label: "research" },
  { id: "retrieve_branch", x: 60, y: 96, label: "retrieve_branch ×N" }, { id: "graph_branch", x: 250, y: 96, label: "graph_branch" }, { id: "rewrite", x: 470, y: 96, label: "rewrite / escalate" },
  { id: "fuse_rerank", x: 150, y: 178, label: "fuse_rerank" }, { id: "evaluate_evidence", x: 340, y: 178, label: "evaluate_evidence" },
  { id: "analyze", x: 20, y: 260, label: "analyze" }, { id: "generate", x: 180, y: 260, label: "generate" }, { id: "verify_facts", x: 340, y: 260, label: "verify_facts" }, { id: "validate_citations", x: 500, y: 260, label: "validate_citations" },
  { id: "quality", x: 340, y: 342, label: "quality" }, { id: "human_review", x: 180, y: 424, label: "human_review" }, { id: "finalize", x: 340, y: 424, label: "finalize" },
];
const EDGES: [string, string, boolean?][] = [
  ["classify", "research"], ["research", "retrieve_branch", true], ["research", "graph_branch", true], ["retrieve_branch", "fuse_rerank"], ["graph_branch", "fuse_rerank"],
  ["fuse_rerank", "evaluate_evidence"], ["evaluate_evidence", "rewrite", true], ["rewrite", "retrieve_branch", true], ["evaluate_evidence", "analyze", true], ["analyze", "generate"],
  ["generate", "verify_facts"], ["verify_facts", "validate_citations"], ["validate_citations", "quality"], ["quality", "rewrite", true], ["quality", "human_review", true],
  ["quality", "finalize", true], ["human_review", "finalize"],
];
const byId = Object.fromEntries(NODES.map((n) => [n.id, n]));

/** The research StateGraph with executed nodes highlighted (count = executions, e.g. retrieval loops). Layout mirrors backend/app/workflows/research_graph.py. */
export function WorkflowGraph({ trace, errors = [], pendingReview = false }: { trace: TraceEntry[]; errors?: string[]; pendingReview?: boolean }) {
  const counts: Record<string, number> = {};
  for (const t of trace) counts[t.node] = (counts[t.node] ?? 0) + 1;
  return (
    <svg viewBox="0 0 660 470" role="img" aria-label="Research workflow graph" style={{ width: "100%", maxHeight: 520 }}>
      <defs><marker id="arr" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="currentColor" opacity="0.6" /></marker></defs>
      {EDGES.map(([a, b, dash], i) => {
        const s = byId[a]!, t = byId[b]!;
        const x1 = s.x + W / 2, y1 = s.y + (t.y >= s.y ? H : 0), x2 = t.x + W / 2, y2 = t.y + (t.y > s.y ? 0 : H);
        const back = t.y <= s.y;
        const d = back ? `M${s.x + W},${s.y + H / 2} C${s.x + W + 70},${s.y + H / 2} ${t.x + W + 70},${t.y + H / 2} ${t.x + W},${t.y + H / 2}` : `M${x1},${y1} L${x2},${y2}`;
        return <path key={i} d={d} className={`wf-edge${dash ? " dash" : ""}`} markerEnd="url(#arr)" />;
      })}
      {NODES.map((n) => {
        const c = counts[n.id] ?? 0;
        const err = errors.some((e) => e.startsWith(`${n.id}:`));
        const cls = err ? "err" : n.id === "human_review" && pendingReview ? "pending" : c > 0 ? "ran" : "";
        return (
          <g key={n.id} className={`wf-node ${cls}`} data-node={n.id} data-count={c}>
            <rect x={n.x} y={n.y} width={W} height={H} />
            <text x={n.x + W / 2} y={n.y + H / 2 + 4} textAnchor="middle" fontSize="11">{n.label}</text>
            {c > 1 && <text x={n.x + W - 6} y={n.y + 11} textAnchor="end" fontSize="10" fontWeight="700">×{c}</text>}
          </g>
        );
      })}
    </svg>
  );
}
