import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Badge, EmptyState, Loading, PageHead } from "../components/ui";
import { useApi } from "../features/auth/AuthContext";
import { useAsync } from "../hooks/useAsync";
import { pageLabel } from "../services/format";
import type { Message } from "../types/schemas";

export interface DocCitations { document_id: string; title: string; filename: string; count: number; pages: number[]; quotes: { quote: string; page: number | null; chunk_id?: string }[] }

/** Aggregates the citations stored on assistant messages by source document. */
export function aggregateCitations(messages: Message[]): DocCitations[] {
  const byDoc = new Map<string, DocCitations>();
  for (const m of messages) {
    if (m.role !== "assistant") continue;
    for (const c of m.citations) {
      if (!c.document_id) continue;
      const d = byDoc.get(c.document_id) ?? { document_id: c.document_id, title: c.title ?? c.document_id, filename: c.filename ?? "", count: 0, pages: [], quotes: [] };
      d.count += 1;
      if (c.page_start && !d.pages.includes(c.page_start)) d.pages.push(c.page_start);
      const q = c.quote ?? c.quotes?.[0];
      if (q && !d.quotes.some((x) => x.quote === q)) d.quotes.push({ quote: q, page: c.page_start ?? null, chunk_id: c.chunk_id });
      byDoc.set(c.document_id, d);
    }
  }
  return [...byDoc.values()].map((d) => ({ ...d, pages: d.pages.sort((a, b) => a - b) })).sort((a, b) => b.count - a.count);
}

export function CitationExplorerPage() {
  const api = useApi();
  const [filter, setFilter] = useState("");
  const [open, setOpen] = useState<string>();
  const data = useAsync(async () => {
    const convs = (await api.conversations()).slice(0, 20);
    return (await Promise.all(convs.map((c) => api.messages(c.id)))).flat();
  }, [api]);
  const docs = useMemo(() => aggregateCitations(data.data ?? []).filter((d) => (d.title + d.filename).toLowerCase().includes(filter.toLowerCase())), [data.data, filter]);
  return (
    <>
      <PageHead title="Citation explorer"><input style={{ width: 240 }} placeholder="Filter documents…" value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Filter documents" /></PageHead>
      <div className="small muted" style={{ marginBottom: 10 }}>Sources your recent answers relied on (last 20 conversations). Every citation links back to the exact passage.</div>
      <div className="card flush">
        <Loading loading={data.loading && !data.data} error={data.error} empty={!docs.length ? <EmptyState title="No citations yet" hint="Citations appear after you ask research questions." /> : undefined}>
          <table className="data"><thead><tr><th>Document</th><th>Times cited</th><th>Pages</th></tr></thead><tbody>
            {docs.flatMap((d) => [
              <tr key={d.document_id} className="clickable" onClick={() => setOpen(open === d.document_id ? undefined : d.document_id)}>
                <td><b>{d.title}</b><div className="small muted">{d.filename}</div></td><td><Badge tone="info">{d.count}</Badge></td><td className="small">{d.pages.length ? d.pages.map((p) => pageLabel(p)).join(", ") : "–"}</td>
              </tr>,
              ...(open === d.document_id ? [<tr key={`${d.document_id}-q`}><td colSpan={3}>
                {d.quotes.map((q, i) => <div key={i} className="quote">“{q.quote}” <span className="small muted">{pageLabel(q.page)}</span> {q.chunk_id && <Link className="small" to={`/documents/${d.document_id}?chunk=${q.chunk_id}`}>open →</Link>}</div>)}
                <div style={{ marginTop: 8 }}><Link className="small" to={`/documents/${d.document_id}`}>Open document →</Link></div>
              </td></tr>] : []),
            ])}
          </tbody></table>
        </Loading>
      </div>
    </>
  );
}
