import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { Badge, EmptyState, ErrorBanner, Loading, PageHead, Tabs } from "../components/ui";
import { useApi } from "../features/auth/AuthContext";
import { useAsync } from "../hooks/useAsync";
import { pageLabel, statusTone } from "../services/format";
import type { Chunk, Figure } from "../types/schemas";

/** Groups chunks by starting page (documents without pages — txt, md, csv — form one group). */
export function groupByPage(chunks: Chunk[]): { page: number | null; chunks: Chunk[] }[] {
  const groups: { page: number | null; chunks: Chunk[] }[] = [];
  for (const c of chunks) {
    const last = groups[groups.length - 1];
    if (last && last.page === c.page_start) last.chunks.push(c);
    else groups.push({ page: c.page_start, chunks: [c] });
  }
  return groups;
}

function FigureThumb({ docId, fig }: { docId: string; fig: Figure }) {
  const api = useApi();
  const [src, setSrc] = useState<string>();
  useEffect(() => {
    let url: string | undefined;
    let live = true;
    api.figureBlobUrl(docId, fig.index).then((u) => { url = u; if (live) setSrc(u); }).catch(() => undefined);
    return () => { live = false; if (url) URL.revokeObjectURL(url); };
  }, [api, docId, fig.index]);
  return (
    <figure style={{ margin: 0 }} data-figure={fig.index}>
      {src ? <img src={src} alt={`Figure ${fig.index}${fig.page ? `, page ${fig.page}` : ""}`} style={{ maxWidth: "100%", border: "1px solid var(--line)" }} /> : <div className="muted small">Loading figure…</div>}
      <figcaption className="small muted">Figure {fig.index}{fig.page ? ` · page ${fig.page}` : ""} · OCR {fig.ocr?.status ?? "off"}{fig.caption ? ` · caption ${fig.caption.status}` : ""}</figcaption>
    </figure>
  );
}

export function DocumentViewerPage() {
  const api = useApi();
  const { id = "" } = useParams();
  const [params] = useSearchParams();
  const highlight = params.get("chunk");
  const [level, setLevel] = useState<"child" | "parent">("child");
  const doc = useAsync(() => api.document(id), [api, id]);
  const chunks = useAsync(() => api.chunks(id, level), [api, id, level]);
  const figures = useAsync(() => api.figures(id).catch(() => [] as Figure[]), [api, id]);
  const contexts = useAsync(() => api.contexts(), [api]);
  const [ctx, setCtx] = useState("");
  const [saved, setSaved] = useState<string[]>([]);
  const [error, setError] = useState<unknown>();
  const hl = useRef<HTMLDivElement | null>(null);
  const groups = useMemo(() => groupByPage(chunks.data ?? []), [chunks.data]);
  const sections = useMemo(() => Array.from(new Set((chunks.data ?? []).map((c) => c.section).filter((s): s is string => !!s))), [chunks.data]);

  useEffect(() => { hl.current?.scrollIntoView({ block: "center" }); }, [chunks.data, highlight]);

  async function save(chunkId: string) {
    if (!ctx) return;
    try { await api.addFinding(ctx, chunkId); setSaved((s) => [...s, chunkId]); } catch (e) { setError(e); }
  }

  return (
    <>
      <PageHead title={doc.data?.title ?? "Document"}>
        <Link className="btn" to="/documents">← Documents</Link>
      </PageHead>
      <ErrorBanner error={error ?? doc.error} />
      {doc.data && (
        <div className="row wrap" style={{ marginBottom: 12 }}>
          <Badge tone={statusTone(doc.data.status)}>{doc.data.status}</Badge><Badge>v{doc.data.current_version}</Badge><Badge>{doc.data.doc_type}</Badge>
          {doc.data.language && <Badge>{doc.data.language}</Badge>}<span className="muted small mono">{doc.data.filename}</span>
        </div>
      )}
      <div className="grid" style={{ gridTemplateColumns: "230px 1fr" }}>
        <div className="card" aria-label="Outline">
          <h3>Outline</h3>
          {sections.length === 0 ? <div className="small muted">No sections detected.</div> : sections.map((s) => (
            <div key={s} className="small" style={{ padding: "3px 0" }}><a href={`#s-${encodeURIComponent(s)}`}>{s}</a></div>
          ))}
          <hr style={{ border: 0, borderTop: "1px solid var(--line)", margin: "12px 0" }} />
          <Tabs tabs={[{ id: "child", label: "Passages" }, { id: "parent", label: "Wide context" }]} active={level} onChange={setLevel} />
          <label htmlFor="ctx">Save passages to research context</label>
          <select id="ctx" value={ctx} onChange={(e) => setCtx(e.target.value)}>
            <option value="">Choose…</option>
            {contexts.data?.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        </div>
        <div className="card flush">
          {!!figures.data?.length && (
            <div className="chunk" aria-label="Figures"><div className="meta">Figures ({figures.data.length})</div>
              <div className="row wrap" style={{ gap: 12 }}>{figures.data.map((f) => <FigureThumb key={f.index} docId={id} fig={f} />)}</div></div>
          )}
          <Loading loading={chunks.loading && !chunks.data} error={chunks.error} empty={chunks.data && !chunks.data.length ? <EmptyState title="No indexed text" hint="The document may still be processing or contain no extractable text." /> : undefined}>
            {groups.map((g, gi) => (
              <div key={gi}>
                {g.page !== null && <div className="chunk muted small" style={{ background: "var(--panel-2)", fontWeight: 600 }}>Page {g.page}</div>}
                {g.chunks.map((c) => {
                  const isHl = c.id === highlight;
                  return (
                    <div key={c.id} id={c.section ? `s-${encodeURIComponent(c.section)}` : undefined} className={`chunk${isHl ? " highlight" : ""}`} ref={isHl ? hl : undefined} data-chunk={c.id}>
                      <div className="meta">#{c.ordinal} {c.section && <>· {c.section} </>}{pageLabel(c.page_start, c.page_end) && <>· {pageLabel(c.page_start, c.page_end)} </>}· {c.token_count} tokens {c.kind !== "text" && <Badge tone="info">{c.kind}</Badge>} {!c.is_current && <Badge tone="warn">older version</Badge>}</div>
                      <div style={{ whiteSpace: "pre-wrap" }}>{c.text}</div>
                      {ctx && level === "child" && <button className="btn small" style={{ marginTop: 6 }} disabled={saved.includes(c.id)} onClick={() => void save(c.id)}>{saved.includes(c.id) ? "Saved" : "Save finding"}</button>}
                    </div>
                  );
                })}
              </div>
            ))}
          </Loading>
        </div>
      </div>
    </>
  );
}
