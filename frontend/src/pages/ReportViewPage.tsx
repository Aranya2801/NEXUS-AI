import { useState, type ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Badge, ErrorBanner, Loading, PageHead, Spinner } from "../components/ui";
import { useApi } from "../features/auth/AuthContext";
import { useAsync, usePoll } from "../hooks/useAsync";
import { downloadBlob, pageLabel, statusTone } from "../services/format";
import type { ReportBlock, ReportContent } from "../types/schemas";

function Cites({ ids }: { ids: number[] }) {
  return <>{ids.map((i) => <a key={i} className="chip" href={`#src-${i}`} aria-label={`Source ${i}`} style={{ textDecoration: "none" }}>{i}</a>)}</>;
}

export function Block({ b }: { b: ReportBlock }): ReactNode {
  switch (b.type) {
    case "claim": return <li>{b.text} <Cites ids={b.citations} />{b.status === "weak" && <em className="small muted"> (limited support)</em>}</li>;
    case "list": return <>{b.items.map((i, k) => <li key={k}>{i.text} <Cites ids={i.citations} /></li>)}</>;
    case "note": return <p className="small muted" style={{ fontStyle: "italic" }}>{b.text}</p>;
    case "table": return (
      <table className="data"><thead><tr>{b.columns.map((c) => <th key={c}>{c}</th>)}</tr></thead><tbody>
        {b.rows.map((r, i) => <tr key={i}>{r.cells.map((c, j) => <td key={j}>{c}{j === r.cells.length - 1 && <> <Cites ids={r.citations} /></>}</td>)}</tr>)}
      </tbody></table>
    );
  }
}

/** Groups consecutive claim/list blocks into one <ul>; tables and notes render standalone. */
export function SectionBody({ blocks }: { blocks: ReportBlock[] }) {
  const out: ReactNode[] = [];
  let bullets: ReportBlock[] = [];
  const flush = () => { if (bullets.length) { out.push(<ul key={`u${out.length}`}>{bullets.map((b, i) => <Block key={i} b={b} />)}</ul>); bullets = []; } };
  blocks.forEach((b, i) => { if (b.type === "claim" || b.type === "list") bullets.push(b); else { flush(); out.push(<Block key={`b${i}`} b={b} />); } });
  flush();
  return <>{out}</>;
}

export function ReportBody({ c }: { c: ReportContent }) {
  return (
    <div className="report">
      {c.redacted && <div className="banner warn">Some sources are no longer accessible to you, so parts of this report are hidden.</div>}
      {c.method.warning && <div className="banner warn">{c.method.warning}</div>}
      <div className="small muted">Topic: {c.topic} · generated {new Date(c.generated_at).toLocaleString()} · {c.documents.length} document(s)</div>
      <h2>Executive summary</h2>
      {c.executive_summary.length ? <ul>{c.executive_summary.map((i, k) => <li key={k}>{i.text} <Cites ids={i.citations} /></li>)}</ul> : <p className="muted">No evidence was found for this topic.</p>}
      {c.sections.map((s) => <section key={s.id}><h2>{s.title}</h2><SectionBody blocks={s.blocks} /></section>)}
      <h2>Documents analysed</h2>
      <ul>{c.documents.map((d) => <li key={d.document_id}><Link to={`/documents/${d.document_id}`}>{d.title}</Link> <span className="muted small">{d.filename} · v{d.versions.join(", ")}</span></li>)}</ul>
      <h2>Sources</h2>
      {c.citations.map((s) => (
        <div key={s.id} id={`src-${s.id}`} className="cite-card">
          <b>[{s.id}] {s.title}</b> <span className="small muted">{s.filename} {pageLabel(s.page_start, s.page_end)} {s.section && `· ${s.section}`} {s.version !== undefined && `· v${s.version}`}</span>
          {(s.quotes?.length ? s.quotes : s.quote ? [s.quote] : []).map((q, i) => <div key={i} className="quote">“{q}”</div>)}
          {s.document_id && !s.hidden && <Link className="small" to={`/documents/${s.document_id}${s.chunk_id ? `?chunk=${s.chunk_id}` : ""}`}>open source →</Link>}
        </div>
      ))}
      <h2>Coverage notes</h2>
      <ul>{c.coverage_notes.map((n, i) => <li key={i} className="small">{n}</li>)}</ul>
      <h2>Method and limitations</h2>
      <p className="small">{c.method.statement}</p>
      <p className="small muted">Verified claims kept: {c.verification.claims_kept} of {c.verification.claims_total} considered · unsupported removed: {c.verification.unsupported_removed.length} · dropped by final numeric check: {c.verification.dropped_at_final_check.length} · model: {c.method.llm}</p>
    </div>
  );
}

export function ReportViewPage() {
  const api = useApi();
  const nav = useNavigate();
  const { id = "" } = useParams();
  const rep = useAsync(() => api.report(id), [api, id]);
  usePoll(rep.reload, 1500, rep.data?.status === "queued" || rep.data?.status === "running");
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState<string>();
  const r = rep.data;

  async function exportAs(fmt: "md" | "docx" | "pdf") {
    setBusy(fmt); setError(undefined);
    try { const { blob, filename } = await api.exportReport(id, fmt); downloadBlob(blob, filename); } catch (e) { setError(e); } finally { setBusy(undefined); }
  }
  async function remove() {
    if (!window.confirm("Delete this report?")) return;
    try { await api.deleteReport(id); nav("/reports"); } catch (e) { setError(e); }
  }

  return (
    <>
      <PageHead title={r?.title ?? "Report"}>
        <Link className="btn" to="/reports">← Reports</Link>
        {r?.status === "ready" && (["pdf", "docx", "md"] as const).map((f) => <button key={f} className="btn" disabled={!!busy} onClick={() => void exportAs(f)}>{busy === f ? <Spinner /> : `Export ${f.toUpperCase()}`}</button>)}
        {r && <button className="btn danger" onClick={() => void remove()}>Delete</button>}
      </PageHead>
      <ErrorBanner error={error} />
      <Loading loading={rep.loading && !r} error={rep.error}>
        {r && (
          <div className="card">
            <div className="row" style={{ marginBottom: 8 }}><Badge tone={statusTone(r.status)}>{r.status}</Badge>{(r.status === "queued" || r.status === "running") && <Spinner label="Generating" />}</div>
            {r.progress.length > 0 && r.status !== "ready" && <ol className="small">{r.progress.map((p, i) => <li key={i}>{p.detail}</li>)}</ol>}
            {r.status === "failed" && <div className="banner" role="alert">Report generation failed: {r.error}</div>}
            {r.content && <ReportBody c={r.content} />}
          </div>
        )}
      </Loading>
    </>
  );
}
