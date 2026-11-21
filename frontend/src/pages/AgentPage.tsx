import { useState, type FormEvent } from "react";
import { Badge, ErrorBanner, PageHead } from "../components/ui";
import { useApi } from "../features/auth/AuthContext";
import type { AgenticRun } from "../types/schemas";

export function AgentPage() {
  const api = useApi();
  const [goal, setGoal] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const [run, setRun] = useState<AgenticRun>();

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true); setError(undefined); setRun(undefined);
    try { setRun(await api.agenticRun(goal.trim())); } catch (err) { setError(err); } finally { setBusy(false); }
  }
  const r = run?.result;

  return (
    <>
      <PageHead title="Autonomous agent" />
      <p className="muted small">The agent plans tool calls (search, graph, document reading, calculator), observes the results, and re-plans within hard step/time budgets. The final answer passes the same claim verification as chat; calculator results are labelled as computed, not cited.</p>
      <form onSubmit={submit} className="row" style={{ gap: 8, marginBottom: 14 }}>
        <input aria-label="Research goal" value={goal} onChange={(e) => setGoal(e.target.value)} placeholder="e.g. Compare the two retention policies and compute the difference in days" style={{ flex: 1 }} />
        <button className="btn primary" disabled={busy || goal.trim().length < 3}>{busy ? "Working…" : "Run"}</button>
      </form>
      <ErrorBanner error={error ?? (run?.status === "failed" ? run.error : undefined)} />
      {r && (
        <div className="col" style={{ gap: 14 }}>
          <div className="card">
            <div className="row wrap" style={{ gap: 8, marginBottom: 6 }}>
              <Badge tone={r.answer_status === "grounded" ? "good" : r.answer_status === "insufficient_evidence" ? "bad" : "warn"}>{r.answer_status}</Badge>
              <Badge>planner: {Array.from(new Set(r.planner)).join(", ") || "none"}</Badge>
              <Badge>{r.budget.steps_used}/{r.budget.max_steps} steps · {r.budget.waves_used}/{r.budget.max_waves} waves</Badge>
              <Badge tone={r.stopped_reason === "planner_done" ? "good" : "warn"}>stopped: {r.stopped_reason}</Badge>
            </div>
            <div style={{ whiteSpace: "pre-wrap" }}>{r.answer}</div>
            {r.citations.map((c) => <div key={c.id} className="small muted">[{c.id}] {c.title}{c.page_start ? ` · p.${c.page_start}` : ""} — “{c.quote}”</div>)}
          </div>
          <div className="card flush" aria-label="Execution trace">
            {r.steps.map((s) => (
              <div className="chunk" key={s.id} data-step={s.id}>
                <div className="meta">wave {s.wave} · <strong>{s.tool}</strong> <Badge tone={s.ok ? "good" : "bad"}>{s.ok ? "ok" : "failed"}</Badge> · {s.ms} ms {s.depends_on.length > 0 && <>· after {s.depends_on.join(", ")}</>}</div>
                <div className="small">{s.purpose}</div>
                <div className="small mono muted">{JSON.stringify(s.args)}</div>
                {s.error ? <div className="small error">{s.error}</div> : <div className="small muted">{s.summary}</div>}
              </div>
            ))}
          </div>
        </div>
      )}
    </>
  );
}
