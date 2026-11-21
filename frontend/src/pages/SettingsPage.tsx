import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { Badge, EmptyState, ErrorBanner, Loading, PageHead, Tabs } from "../components/ui";
import { useApi, useAuth } from "../features/auth/AuthContext";
import { useAsync } from "../hooks/useAsync";
import { relTime } from "../services/format";

type Tab = "preferences" | "contexts" | "memory" | "history";

export function SettingsPage() {
  const api = useApi();
  const { user } = useAuth();
  const [tab, setTab] = useState<Tab>("preferences");
  const [error, setError] = useState<unknown>();
  const [notice, setNotice] = useState<string>();
  const prefs = useAsync(() => api.memory("long_term"), [api]);
  const all = useAsync(() => api.memory(), [api]);
  const contexts = useAsync(() => api.contexts(), [api]);
  const recent = useAsync(() => api.recentDocuments(), [api]);
  const [key, setKey] = useState("");
  const [val, setVal] = useState("");
  const [cname, setCname] = useState("");
  const [open, setOpen] = useState<string>();
  const detail = useAsync(() => (open ? api.context(open) : Promise.resolve(undefined)), [api, open]);
  const counts = (all.data ?? []).reduce<Record<string, number>>((m, i) => ({ ...m, [i.kind]: (m[i.kind] ?? 0) + 1 }), {});

  async function addPref(e: FormEvent) {
    e.preventDefault();
    try { await api.setPreference(key.trim(), val); setKey(""); setVal(""); prefs.reload(); all.reload(); } catch (err) { setError(err); }
  }
  async function forget(kind?: string, withConversations = false) {
    const what = withConversations ? "ALL memory AND all conversations, runs and saved workflow checkpoints" : kind ? `all "${kind}" memory` : "all memory";
    if (!window.confirm(`Permanently delete ${what}? This cannot be undone.`)) return;
    try { const r = await api.forget(kind, withConversations); setNotice(`Deleted ${r.deleted_items} memory item(s)${withConversations ? ` and ${r.deleted_conversations} conversation(s)` : ""}.`); all.reload(); prefs.reload(); recent.reload(); contexts.reload(); }
    catch (err) { setError(err); }
  }

  return (
    <>
      <PageHead title="Settings & memory" />
      <div className="card" style={{ marginBottom: 14 }}>
        <div className="row wrap"><b>{user?.full_name || user?.email}</b><Badge tone="info">{user?.role}</Badge><span className="muted small">{user?.email}</span></div>
        <div className="small muted" style={{ marginTop: 6 }}>Everything NEXUS remembers about you is listed here and is private to you — even organisation admins cannot read it. Do not store secrets in memory.</div>
      </div>
      <ErrorBanner error={error} />
      {notice && <div className="banner info" role="status">{notice}</div>}
      <Tabs<Tab> active={tab} onChange={setTab} tabs={[{ id: "preferences", label: "Preferences" }, { id: "contexts", label: "Research contexts" }, { id: "memory", label: "All memory" }, { id: "history", label: "Document history" }]} />

      {tab === "preferences" && (
        <div className="card">
          <p className="small muted">Preferences are passed to the answer writer as style hints (wording and format only); they never override the evidence rules.</p>
          <form className="row" onSubmit={(e) => void addPref(e)} aria-label="Add preference">
            <input value={key} onChange={(e) => setKey(e.target.value)} placeholder="key, e.g. answer_style" aria-label="Preference key" /><input value={val} onChange={(e) => setVal(e.target.value)} placeholder="value, e.g. concise bullet points" aria-label="Preference value" />
            <button className="btn primary" type="submit" disabled={!key.trim() || !val.trim()}>Save</button>
          </form>
          <Loading loading={prefs.loading && !prefs.data} error={prefs.error} empty={prefs.data && !prefs.data.length ? <EmptyState title="No preferences saved" /> : undefined}>
            <table className="data"><tbody>{prefs.data?.map((p) => <tr key={p.id}><td className="mono">{p.key}</td><td>{String(p.value)}</td><td style={{ textAlign: "right" }}><button className="btn small danger" onClick={() => api.deleteMemoryItem(p.id).then(() => { prefs.reload(); all.reload(); }, setError)}>Delete</button></td></tr>)}</tbody></table>
          </Loading>
        </div>
      )}

      {tab === "contexts" && (
        <div className="grid cols-2">
          <div className="card">
            <p className="small muted">A research context pins documents and saves page-cited findings. Chat inside a context only searches its documents.</p>
            <form className="row" onSubmit={(e) => { e.preventDefault(); api.createContext(cname.trim()).then(() => { setCname(""); contexts.reload(); }, setError); }}>
              <input value={cname} onChange={(e) => setCname(e.target.value)} placeholder="New context name" aria-label="Context name" /><button className="btn primary" type="submit" disabled={!cname.trim()}>Create</button>
            </form>
            <Loading loading={contexts.loading && !contexts.data} error={contexts.error} empty={contexts.data && !contexts.data.length ? <EmptyState title="No research contexts" /> : undefined}>
              {contexts.data?.map((c) => (
                <div key={c.id} className="row" style={{ padding: "8px 0", borderBottom: "1px solid var(--line)" }}>
                  <button className="btn ghost grow" style={{ textAlign: "left" }} onClick={() => setOpen(c.id)}>{c.name} <span className="small muted">· {c.pinned_documents} pinned</span></button>
                  <button className="btn small danger" onClick={() => api.deleteContext(c.id).then(() => { setOpen(undefined); contexts.reload(); }, setError)}>Delete</button>
                </div>
              ))}
            </Loading>
          </div>
          <div className="card">
            {!open ? <EmptyState title="Select a context" /> : (
              <Loading loading={detail.loading && !detail.data} error={detail.error}>
                {detail.data && <><h2>{detail.data.name}</h2><div className="small muted">{detail.data.goal}</div>
                  {detail.data.findings.length === 0 && <EmptyState title="No findings saved" hint="Open a document and save passages." />}
                  {detail.data.findings.map((f) => f.hidden ? <div key={f.id} className="cite-card muted small">🔒 A saved finding is hidden: {f.reason}.</div> :
                    <div key={f.id} className="cite-card"><b>{f.title}</b> <span className="small muted">p.{f.page_start ?? "–"}</span><div className="quote">{f.quote}</div>{f.note && <div className="small">Note: {f.note}</div>}{f.document_id && <Link className="small" to={`/documents/${f.document_id}?chunk=${f.chunk_id}`}>open →</Link>}</div>)}
                </>}
              </Loading>
            )}
          </div>
        </div>
      )}

      {tab === "memory" && (
        <div className="card">
          <div className="row wrap" style={{ marginBottom: 10 }}>{Object.entries(counts).map(([k, n]) => <Badge key={k}>{k}: {n}</Badge>)}
            <span className="right row"><button className="btn small" onClick={() => void forget("session")}>Clear session memory</button><button className="btn small" onClick={() => void forget("doc_interaction")}>Clear document history</button>
              <button className="btn small danger" onClick={() => void forget(undefined, true)}>Delete everything…</button></span></div>
          <Loading loading={all.loading && !all.data} error={all.error} empty={all.data && !all.data.length ? <EmptyState title="Nothing stored" /> : undefined}>
            <table className="data"><thead><tr><th>Kind</th><th>Key</th><th>Expires</th><th /></tr></thead><tbody>
              {all.data?.map((i) => <tr key={i.id}><td><Badge>{i.kind}</Badge></td><td className="mono small">{i.key.length > 28 ? `${i.key.slice(0, 28)}…` : i.key}</td><td className="small muted">{i.expires_at ? relTime(i.expires_at).replace("ago", "") + "(left)" : "never"}</td>
                <td style={{ textAlign: "right" }}><button className="btn small danger" onClick={() => api.deleteMemoryItem(i.id).then(all.reload, setError)}>Delete</button></td></tr>)}
            </tbody></table>
          </Loading>
        </div>
      )}

      {tab === "history" && (
        <div className="card flush"><Loading loading={recent.loading && !recent.data} error={recent.error} empty={recent.data && !recent.data.length ? <EmptyState title="No document history" /> : undefined}>
          <table className="data"><thead><tr><th>Document</th><th>Last action</th><th>Times</th><th>When</th></tr></thead><tbody>
            {recent.data?.map((d) => <tr key={d.document_id}><td><Link to={`/documents/${d.document_id}`}>{d.title}</Link></td><td><Badge>{d.action}</Badge></td><td>{d.count}</td><td className="muted small">{relTime(d.last_at)}</td></tr>)}
          </tbody></table></Loading></div>
      )}
    </>
  );
}
