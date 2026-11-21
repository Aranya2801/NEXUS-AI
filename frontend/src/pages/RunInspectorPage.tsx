import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { AgentTimeline } from "../components/AgentTimeline";
import { WorkflowGraph } from "../components/WorkflowGraph";
import { Badge, ErrorBanner, Json, Loading, Meter, PageHead } from "../components/ui";
import { useApi } from "../features/auth/AuthContext";
import { ReviewBanner } from "../features/chat/ReviewBanner";
import { useAsync } from "../hooks/useAsync";
import { answerTone, ms, pct, statusTone } from "../services/format";

export function RunInspectorPage() {
  const api = useApi();
  const { id = "" } = useParams();
  const run = useAsync(() => api.run(id), [api, id]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const r = run.data;
  const loops = r ? r.trace.filter((t) => t.node === "rewrite").length : 0;
  const pending = r?.status === "needs_review";

  async function decide(d: "approve" | "edit" | "reject", answer?: string, note?: string) {
    setBusy(true); setError(undefined);
    try { await api.resume(id, d, answer, note); run.reload(); } catch (e) { setError(e); } finally { setBusy(false); }
  }

  return (
    <>
      <PageHead title="Agent run"><Link className="btn" to="/runs">← All runs</Link></PageHead>
      <ErrorBanner error={error} />
      <Loading loading={run.loading && !r} error={run.error}>
        {r && (
          <div className="col" style={{ gap: 14 }}>
            <div className="card">
              <div className="row wrap"><b className="grow">{r.query}</b><Badge tone={statusTone(r.status)}>{r.status}</Badge>{r.answer_status && <Badge tone={answerTone(r.answer_status)}>{r.answer_status}</Badge>}<Badge tone="info">{r.query_class}</Badge><Badge title="Re-retrieval loops">{loops} retrieval loop{loops === 1 ? "" : "s"}</Badge></div>
              <AgentTimeline trace={r.trace} reviewPending={pending} />
            </div>
            {pending && r.pending_review?.[0] && <ReviewBanner answer={String(r.pending_review[0]["answer"] ?? r.answer)} reasons={(r.pending_review[0]["reasons"] as string[] | undefined) ?? []} busy={busy} onDecide={(d, a, n) => void decide(d, a, n)} />}
            <div className="grid cols-2">
              <div className="card"><h2>Workflow</h2><WorkflowGraph trace={r.trace} errors={r.errors} pendingReview={pending} /><div className="small muted">Green = executed (×n = repeated), amber = paused for review, red = failed. Dashed edges are conditional.</div></div>
              <div className="card col">
                <h2>Outcome</h2>
                <div className="answer">{r.answer || <span className="muted">No answer yet</span>}</div>
                <div><span className="small muted">Evidence confidence</span><Meter value={r.confidence.score} label="Evidence confidence" /><span className="small mono">{pct(r.confidence.score)}</span></div>
                {(["relevance", "completeness", "groundedness"] as const).map((k) => <div key={k}><span className="small muted">{k}</span><Meter value={r.quality[k]} label={k} /></div>)}
                <div className="small muted">Retrieval queries: {r.queries.join(" · ")}</div>
                {r.errors.length > 0 && <div className="banner">{r.errors.join("; ")}</div>}
                {r.error && <div className="banner">{r.error}</div>}
              </div>
            </div>
            <div className="card flush">
              <h2 style={{ padding: "14px 14px 0" }}>Execution trace ({r.trace.length} steps)</h2>
              <table className="data"><thead><tr><th>#</th><th>Agent</th><th>Node</th><th>Time</th><th>Detail</th></tr></thead><tbody>
                {r.trace.map((t, i) => {
                  const { agent, node, ms: took, ...rest } = t;
                  return <tr key={i}><td className="muted">{i + 1}</td><td>{agent}</td><td className="mono">{node}</td><td>{ms(took)}</td><td>{Object.keys(rest).length ? <details><summary className="small">details</summary><Json value={rest} /></details> : <span className="muted">–</span>}</td></tr>;
                })}
              </tbody></table>
            </div>
            <div className="grid cols-2">
              <div className="card"><h2>Plan</h2><Json value={r.plan} /></div>
              <div className="card"><h2>Resources</h2><Json value={{ llm_usage: r.llm_usage, checkpointed_state_keys: r.state_keys, next: r.next }} /></div>
            </div>
          </div>
        )}
      </Loading>
    </>
  );
}
