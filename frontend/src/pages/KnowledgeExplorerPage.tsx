import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { GraphView } from "../components/GraphView";
import { Badge, EmptyState, ErrorBanner, Loading, PageHead, Spinner, Tabs } from "../components/ui";
import { useApi } from "../features/auth/AuthContext";
import { useAsync } from "../hooks/useAsync";
import { pageLabel, pct } from "../services/format";
import type { GraphQuery } from "../types/schemas";

export function KnowledgeExplorerPage() {
  const api = useApi();
  const [q, setQ] = useState("");
  const [term, setTerm] = useState("");
  const [tab, setTab] = useState<"query" | "entity">("query");
  const [selected, setSelected] = useState<string>();
  const [gq, setGq] = useState("");
  const [depth, setDepth] = useState(2);
  const [graph, setGraph] = useState<GraphQuery>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const ents = useAsync(() => api.entities(term || undefined), [api, term]);
  const detail = useAsync(() => (selected ? api.entity(selected) : Promise.resolve(undefined)), [api, selected]);

  async function runQuery(e: FormEvent) {
    e.preventDefault();
    if (!gq.trim()) return;
    setBusy(true); setError(undefined);
    try { setGraph(await api.graphQuery(gq.trim(), depth)); } catch (err) { setError(err); } finally { setBusy(false); }
  }
  const pick = (id: string) => { setSelected(id); setTab("entity"); };
  const names = graph ? Object.fromEntries(Object.values(graph.nodes).map((n) => [n.id, n.name ?? n.id])) : {};

  return (
    <>
      <PageHead title="Knowledge explorer" />
      <div className="grid" style={{ gridTemplateColumns: "280px 1fr" }}>
        <div className="card flush" aria-label="Entities">
          <form className="row" style={{ padding: 10 }} onSubmit={(e) => { e.preventDefault(); setTerm(q.trim()); }}>
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Find an entity…" aria-label="Find an entity" />
            <button className="btn" type="submit">Find</button>
          </form>
          <Loading loading={ents.loading && !ents.data} error={ents.error} empty={ents.data && !ents.data.length ? <EmptyState title="No entities" hint="Entities are extracted when documents are processed." /> : undefined}>
            <div style={{ maxHeight: 560, overflowY: "auto" }}>
              {ents.data?.map((e) => (
                <button key={e.id} className="btn ghost" style={{ display: "flex", width: "100%", borderRadius: 0, justifyContent: "space-between", background: e.id === selected ? "var(--accent-soft)" : undefined }} onClick={() => pick(e.id)}>
                  <span>{e.name}</span><span className="small muted">{e.type} · {e.mentions}</span>
                </button>
              ))}
            </div>
          </Loading>
        </div>
        <div className="card">
          <Tabs tabs={[{ id: "query", label: "Graph query" }, { id: "entity", label: "Entity" }]} active={tab} onChange={setTab} />
          <ErrorBanner error={error} />
          {tab === "query" && (
            <>
              <form className="row" onSubmit={(e) => void runQuery(e)}>
                <input className="grow" value={gq} onChange={(e) => setGq(e.target.value)} placeholder="e.g. improvements to the Transformer" aria-label="Graph query" />
                <select value={depth} onChange={(e) => setDepth(Number(e.target.value))} aria-label="Hops" style={{ width: "auto" }}>{[1, 2, 3].map((d) => <option key={d} value={d}>{d} hop{d > 1 ? "s" : ""}</option>)}</select>
                <button className="btn primary" type="submit" disabled={busy || !gq.trim()}>{busy ? <Spinner /> : "Explore"}</button>
              </form>
              {graph && (graph.seeds.length === 0 ? <EmptyState title="No matching entities" hint="Try a name that appears in your documents." /> : (
                <>
                  <div className="row wrap small" style={{ margin: "10px 0" }}>Seeds: {graph.seeds.map((s) => <Badge key={s.id} tone="info">{s.name}</Badge>)}<span className="muted">· {Object.keys(graph.nodes).length} entities · {graph.edges.length} relations · {graph.documents} document(s)</span></div>
                  <GraphView nodes={graph.nodes} edges={graph.edges} onSelect={pick} />
                  <table className="data"><thead><tr><th>Relation</th><th>Evidence</th><th /></tr></thead><tbody>
                    {graph.edges.slice(0, 40).map((e, i) => (
                      <tr key={i}><td><b>{names[e.source_id]}</b> <Badge>{e.predicate}</Badge> <b>{names[e.target_id]}</b><div className="small muted">hop {e.depth} · confidence {pct(e.confidence)}</div></td>
                        <td className="small">{e.evidence && <div className="quote">“{e.evidence}”</div>}</td>
                        <td><Link className="small" to={`/documents/${e.document_id}?chunk=${e.chunk_id}`}>{e.title} {pageLabel(e.page)}</Link></td></tr>
                    ))}
                  </tbody></table>
                </>
              ))}
              {!graph && <EmptyState title="Trace connections across documents" hint="Type a topic; NEXUS finds matching entities and follows relations up to three hops, with the sentence that supports each link." />}
            </>
          )}
          {tab === "entity" && (
            <Loading loading={detail.loading && !!selected} error={detail.error} empty={!selected ? <EmptyState title="Select an entity" /> : undefined}>
              {detail.data && (
                <div className="col">
                  <div className="row"><h2>{detail.data.name}</h2><Badge tone="info">{detail.data.type}</Badge></div>
                  {detail.data.aliases.length > 0 && <div className="small muted">Also known as: {detail.data.aliases.join(", ")}</div>}
                  <h3>Relations ({detail.data.edges.length})</h3>
                  {detail.data.edges.map((e, i) => <div key={i} className="cite-card"><b>{e.source}</b> <Badge>{e.predicate}</Badge> <b>{e.target}</b>{e.evidence && <div className="quote">“{e.evidence}”</div>}<div className="small muted">{e.title} {pageLabel(e.page)}</div></div>)}
                  <h3>Mentioned in ({detail.data.mentions.length})</h3>
                  {detail.data.mentions.map((m) => <div key={m.chunk_id} className="small"><Link to={`/documents/${m.document_id}?chunk=${m.chunk_id}`}>{m.title}</Link> {pageLabel(m.page)} {m.section && `· ${m.section}`}</div>)}
                </div>
              )}
            </Loading>
          )}
        </div>
      </div>
    </>
  );
}
