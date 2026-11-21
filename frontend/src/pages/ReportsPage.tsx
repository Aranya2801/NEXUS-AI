import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { Badge, EmptyState, ErrorBanner, Loading, PageHead, Spinner } from "../components/ui";
import { useApi } from "../features/auth/AuthContext";
import { useAsync, usePoll } from "../hooks/useAsync";
import { relTime, statusTone, truncate } from "../services/format";

export function ReportsPage() {
  const api = useApi();
  const nav = useNavigate();
  const list = useAsync(() => api.reports(), [api]);
  usePoll(list.reload, 3000, !!list.data?.some((r) => r.status === "queued" || r.status === "running"));
  const [topic, setTopic] = useState("");
  const [title, setTitle] = useState("");
  const [maxDocs, setMaxDocs] = useState(8);
  const [graph, setGraph] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();

  async function create(e: FormEvent) {
    e.preventDefault();
    setBusy(true); setError(undefined);
    try { const r = await api.createReport({ topic: topic.trim(), title: title.trim() || undefined, max_documents: maxDocs, include_graph: graph }); nav(`/reports/${r.id}`); }
    catch (err) { setError(err); } finally { setBusy(false); }
  }

  return (
    <>
      <PageHead title="Research reports" />
      <form className="card" onSubmit={(e) => void create(e)} aria-label="New report" style={{ marginBottom: 14 }}>
        <h2>New report</h2>
        <ErrorBanner error={error} />
        <div className="grid cols-2" style={{ marginTop: 8 }}>
          <div className="field"><label htmlFor="topic">Topic</label><input id="topic" value={topic} onChange={(e) => setTopic(e.target.value)} placeholder="e.g. transformer architectures" required minLength={3} /></div>
          <div className="field"><label htmlFor="rtitle">Title (optional)</label><input id="rtitle" value={title} onChange={(e) => setTitle(e.target.value)} /></div>
        </div>
        <div className="row wrap">
          <label className="row" style={{ margin: 0 }}>Max documents <input type="number" min={1} max={15} value={maxDocs} onChange={(e) => setMaxDocs(Number(e.target.value))} style={{ width: 70 }} /></label>
          <label className="row" style={{ margin: 0 }}><input type="checkbox" checked={graph} onChange={(e) => setGraph(e.target.checked)} /> Use the knowledge graph</label>
          <button className="btn primary right" type="submit" disabled={busy || topic.trim().length < 3}>{busy ? <Spinner /> : "Generate report"}</button>
        </div>
        <div className="small muted" style={{ marginTop: 8 }}>Every statement is a verified claim or a verbatim source sentence with its page number; contradiction and gap sections are heuristic and labelled.</div>
      </form>
      <div className="card flush">
        <Loading loading={list.loading && !list.data} error={list.error} empty={list.data && !list.data.length ? <EmptyState title="No reports yet" /> : undefined}>
          <table className="data"><thead><tr><th>Title</th><th>Topic</th><th>Status</th><th>Created</th></tr></thead><tbody>
            {list.data?.map((r) => (
              <tr key={r.id} className="clickable" onClick={() => nav(`/reports/${r.id}`)}><td>{truncate(r.title, 60)}</td><td className="muted">{truncate(r.topic, 50)}</td>
                <td><Badge tone={statusTone(r.status)}>{r.status}</Badge></td><td className="muted small">{relTime(r.created_at)}</td></tr>
            ))}
          </tbody></table>
        </Loading>
      </div>
    </>
  );
}
