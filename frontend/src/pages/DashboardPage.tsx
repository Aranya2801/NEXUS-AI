import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Badge, EmptyState, Loading, PageHead, Stat } from "../components/ui";
import { useApi, useAuth } from "../features/auth/AuthContext";
import { useAsync } from "../hooks/useAsync";
import { relTime, statusTone, truncate } from "../services/format";

export function DashboardPage() {
  const api = useApi();
  const { user } = useAuth();
  const nav = useNavigate();
  const [q, setQ] = useState("");
  const convs = useAsync(() => api.conversations(), [api]);
  const runs = useAsync(() => api.runs(), [api]);
  const docs = useAsync(() => api.documents(undefined, 200), [api]);
  const reports = useAsync(() => api.reports(), [api]);
  const recent = useAsync(() => api.recentDocuments(), [api]);
  const admin = useAsync(() => (user?.role === "admin" ? api.adminOverview() : Promise.resolve(undefined)), [api, user?.role]);

  const ask = (e: FormEvent) => { e.preventDefault(); if (q.trim()) nav("/chat", { state: { prompt: q.trim() } }); };
  const ready = docs.data?.filter((d) => d.status === "ready").length ?? 0;
  const pending = docs.data?.filter((d) => d.status === "pending" || d.status === "processing").length ?? 0;
  const needsReview = runs.data?.filter((r) => r.status === "needs_review") ?? [];

  return (
    <>
      <PageHead title={`Welcome${user?.full_name ? `, ${user.full_name}` : ""}`} />
      <form className="card row" onSubmit={ask} aria-label="Ask a research question" style={{ marginBottom: 14 }}>
        <input className="grow" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Ask a question across your documents…" aria-label="Research question" />
        <button className="btn primary" type="submit" disabled={!q.trim()}>Ask</button>
      </form>

      {needsReview.length > 0 && (
        <div className="banner warn" role="status">
          {needsReview.length} answer(s) waiting for human review: {needsReview.map((r) => <Link key={r.run_id} to={`/runs/${r.run_id}`} style={{ marginRight: 8 }}>{truncate(r.query, 40)}</Link>)}
        </div>
      )}

      <div className="grid cols-4" style={{ marginBottom: 14 }}>
        <div className="card"><Stat label="Documents ready" value={ready} hint={pending ? `${pending} processing` : undefined} /></div>
        <div className="card"><Stat label="Conversations" value={convs.data?.length ?? "–"} /></div>
        <div className="card"><Stat label="Reports" value={reports.data?.length ?? "–"} hint={`${reports.data?.filter((r) => r.status === "ready").length ?? 0} ready`} /></div>
        <div className="card"><Stat label="Agent runs" value={runs.data?.length ?? "–"} /></div>
      </div>

      {admin.data && (
        <div className="card" style={{ marginBottom: 14 }}>
          <h2>Organization health</h2>
          <div className="row wrap" style={{ marginTop: 8 }}>
            <Badge tone="info">{admin.data.counts.users} users</Badge><Badge>{admin.data.counts.chunks} chunks</Badge>
            <Badge>{admin.data.graph.entities} entities</Badge><Badge tone={(admin.data.ingestion_jobs["dead"] ?? 0) > 0 ? "bad" : "good"}>{admin.data.ingestion_jobs["dead"] ?? 0} failed jobs</Badge>
            <Badge title="Thumbs up / down on answers">👍 {admin.data.feedback.up} · 👎 {admin.data.feedback.down}</Badge>
            <Link className="right small" to="/admin">Open administration →</Link>
          </div>
        </div>
      )}

      <div className="grid cols-2">
        <div className="card flush">
          <h2 style={{ padding: "14px 14px 0" }}>Recent conversations</h2>
          <Loading loading={convs.loading} error={convs.error} empty={!convs.data?.length ? <EmptyState title="No conversations yet" hint="Ask your first question above." /> : undefined}>
            <table className="data"><tbody>
              {convs.data?.slice(0, 6).map((c) => (
                <tr key={c.id} className="clickable" onClick={() => nav(`/chat/${c.id}`)}><td>{truncate(c.title, 60)}</td><td className="muted small" style={{ textAlign: "right" }}>{relTime(c.updated_at)}</td></tr>
              ))}
            </tbody></table>
          </Loading>
        </div>
        <div className="card flush">
          <h2 style={{ padding: "14px 14px 0" }}>Recently used documents</h2>
          <Loading loading={recent.loading} error={recent.error} empty={!recent.data?.length ? <EmptyState title="Nothing yet" hint="Documents appear here when you cite or open them." /> : undefined}>
            <table className="data"><tbody>
              {recent.data?.slice(0, 6).map((d) => (
                <tr key={d.document_id} className="clickable" onClick={() => nav(`/documents/${d.document_id}`)}><td>{d.title}</td><td><Badge>{d.action}</Badge></td><td className="muted small">{relTime(d.last_at)}</td></tr>
              ))}
            </tbody></table>
          </Loading>
        </div>
        <div className="card flush">
          <h2 style={{ padding: "14px 14px 0" }}>Latest reports</h2>
          <Loading loading={reports.loading} error={reports.error} empty={!reports.data?.length ? <EmptyState title="No reports yet" action={<Link className="btn" to="/reports">Create a report</Link>} /> : undefined}>
            <table className="data"><tbody>
              {reports.data?.slice(0, 5).map((r) => (
                <tr key={r.id} className="clickable" onClick={() => nav(`/reports/${r.id}`)}><td>{truncate(r.title, 50)}</td><td><Badge tone={statusTone(r.status)}>{r.status}</Badge></td></tr>
              ))}
            </tbody></table>
          </Loading>
        </div>
        <div className="card flush">
          <h2 style={{ padding: "14px 14px 0" }}>Latest agent runs</h2>
          <Loading loading={runs.loading} error={runs.error} empty={!runs.data?.length ? <EmptyState title="No runs yet" /> : undefined}>
            <table className="data"><tbody>
              {runs.data?.slice(0, 5).map((r) => (
                <tr key={r.run_id} className="clickable" onClick={() => nav(`/runs/${r.run_id}`)}><td>{truncate(r.query, 50)}</td><td><Badge tone={statusTone(r.status)}>{r.status}</Badge></td></tr>
              ))}
            </tbody></table>
          </Loading>
        </div>
      </div>
    </>
  );
}
