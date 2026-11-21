export const AGENTS: { id: string; label: string }[] = [
  { id: "orchestrator", label: "Orchestrator" }, { id: "research", label: "Research" }, { id: "retrieval", label: "Retrieval" },
  { id: "knowledge_graph", label: "Knowledge graph" }, { id: "analytical", label: "Analytical" }, { id: "report", label: "Report" },
  { id: "verification", label: "Verification" }, { id: "quality_control", label: "Quality control" }, { id: "human", label: "Human review" },
];

interface Props {
  trace: { agent: string | null; node: string }[];
  running?: boolean;
  selected?: string[];
  reviewPending?: boolean;
}

/** Shows which specialist agents have run (count of node executions), which is active, and which were not needed. */
export function AgentTimeline({ trace, running = false, selected, reviewPending = false }: Props) {
  const counts: Record<string, number> = {};
  for (const t of trace) if (t.agent) counts[t.agent] = (counts[t.agent] ?? 0) + 1;
  const last = trace.length ? trace[trace.length - 1]?.agent : null;
  return (
    <ol className="timeline" aria-label="Agent progress" style={{ listStyle: "none", padding: 0 }}>
      {AGENTS.map((a) => {
        const n = counts[a.id] ?? 0;
        let state = "idle";
        if (a.id === "human" && reviewPending) state = "active";
        else if (running && last === a.id) state = "active";
        else if (n > 0) state = "done";
        else if (!running && selected && !selected.includes(a.id) && a.id !== "human") state = "skipped";
        return (
          <li key={a.id} className={`step ${state}`} data-state={state} data-agent={a.id}>
            <span className="dot" />
            {a.label}
            {n > 1 && <span className="mono small">×{n}</span>}
          </li>
        );
      })}
    </ol>
  );
}
