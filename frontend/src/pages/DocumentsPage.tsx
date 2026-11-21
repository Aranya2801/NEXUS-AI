import { useEffect, useRef, useState, type DragEvent } from "react";
import { useNavigate } from "react-router-dom";
import { Badge, EmptyState, ErrorBanner, Loading, Modal, PageHead, Spinner } from "../components/ui";
import { useApi, useAuth } from "../features/auth/AuthContext";
import { useAsync, usePoll } from "../hooks/useAsync";
import { ALLOWED_EXTENSIONS, MAX_UPLOAD_MB } from "../services/api";
import { relTime, statusTone } from "../services/format";
import type { Upload } from "../types/schemas";

/** Client-side pre-check (the server validates again: size, allow-list, magic bytes). Returns a reason (without the filename) or null. */
export function validateFile(file: { name: string; size: number }): string | null {
  const ext = file.name.slice(file.name.lastIndexOf(".")).toLowerCase();
  if (!ALLOWED_EXTENSIONS.includes(ext)) return `file type "${ext || "none"}" is not supported`;
  if (file.size === 0) return "file is empty";
  if (file.size > MAX_UPLOAD_MB * 1024 * 1024) return `larger than ${MAX_UPLOAD_MB} MB`;
  return null;
}

interface Result { name: string; ok: boolean; text: string }

export function DocumentsPage() {
  const api = useApi();
  const { user } = useAuth();
  const nav = useNavigate();
  const canWrite = user?.role === "editor" || user?.role === "admin";
  const cols = useAsync(() => api.collections(), [api]);
  const [sel, setSel] = useState<string>();
  const [results, setResults] = useState<Result[]>([]);
  const [busy, setBusy] = useState(false);
  const [drag, setDrag] = useState(false);
  const [modal, setModal] = useState<"collection" | "access" | null>(null);
  const [error, setError] = useState<unknown>();
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => { if (!sel && cols.data?.[0]) setSel(cols.data[0].id); }, [cols.data, sel]);
  const docs = useAsync(() => (sel ? api.documents(sel) : Promise.resolve([])), [api, sel]);
  usePoll(docs.reload, 2000, !!docs.data?.some((d) => d.status === "pending" || d.status === "processing"));
  const current = cols.data?.find((c) => c.id === sel);

  async function upload(files: FileList | File[]) {
    if (!sel) return;
    setBusy(true);
    const out: Result[] = [];
    for (const f of Array.from(files)) {
      const bad = validateFile(f);
      if (bad) { out.push({ name: f.name, ok: false, text: bad }); continue; }
      try {
        const r: Upload = await api.uploadDocument(sel, f);
        out.push({ name: f.name, ok: true, text: r.outcome === "duplicate" ? "Already uploaded (identical content)" : r.outcome === "new_version" ? `New version v${r.document.current_version + 1} processing` : "Uploaded, processing" });
      } catch (e) {
        out.push({ name: f.name, ok: false, text: e instanceof Error ? e.message : String(e) });
      }
    }
    setResults(out);
    setBusy(false);
    docs.reload();
  }

  const onDrop = (e: DragEvent) => { e.preventDefault(); setDrag(false); if (canWrite && e.dataTransfer.files.length) void upload(e.dataTransfer.files); };

  async function remove(id: string, title: string) {
    if (!window.confirm(`Delete "${title}" and everything derived from it (chunks, graph entities)?`)) return;
    try { await api.deleteDocument(id); docs.reload(); } catch (e) { setError(e); }
  }
  async function showError(id: string) {
    try { const j = await api.documentJob(id); setError(new Error(j.error ?? `Job ${j.status} at stage ${j.stage}`)); } catch (e) { setError(e); }
  }

  return (
    <>
      <PageHead title="Documents">
        {canWrite && <button className="btn" onClick={() => setModal("collection")}>New collection</button>}
        {user?.role === "admin" && current?.is_restricted && <button className="btn" onClick={() => setModal("access")}>Manage access</button>}
      </PageHead>
      <ErrorBanner error={error ?? cols.error} />
      <div className="grid" style={{ gridTemplateColumns: "220px 1fr" }}>
        <div className="card flush" aria-label="Collections">
          <Loading loading={cols.loading} empty={!cols.data?.length ? <EmptyState title="No collections" hint={canWrite ? "Create one to start uploading." : "Ask an editor to create one."} /> : undefined}>
            {cols.data?.map((c) => (
              <button key={c.id} className="btn ghost" style={{ display: "flex", width: "100%", justifyContent: "space-between", borderRadius: 0, textAlign: "left", background: c.id === sel ? "var(--accent-soft)" : undefined }} onClick={() => setSel(c.id)}>
                <span>{c.name}</span>{c.is_restricted && <Badge tone="warn" title="Visible only to people granted access">restricted</Badge>}
              </button>
            ))}
          </Loading>
        </div>
        <div className="col">
          {canWrite && sel && (
            <div className="card" onDragOver={(e) => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)} onDrop={onDrop}
              style={{ borderStyle: "dashed", background: drag ? "var(--accent-soft)" : undefined, textAlign: "center" }}>
              <input ref={fileRef} type="file" multiple hidden aria-label="Choose files" accept={ALLOWED_EXTENSIONS.join(",")} onChange={(e) => e.target.files && void upload(e.target.files)} />
              <div>{busy ? <><Spinner /> Uploading…</> : <>Drop files here or <button className="btn small" onClick={() => fileRef.current?.click()}>browse</button></>}</div>
              <div className="small muted">PDF, DOCX, PPTX, XLSX, TXT, MD, HTML, CSV · up to {MAX_UPLOAD_MB} MB</div>
              {results.map((r, i) => <div key={i} className={`small ${r.ok ? "" : "muted"}`} style={{ color: r.ok ? "var(--good)" : "var(--bad)" }}>{r.name}: {r.text}</div>)}
            </div>
          )}
          <div className="card flush">
            <Loading loading={docs.loading && !docs.data} error={docs.error} empty={docs.data && !docs.data.length ? <EmptyState title="No documents in this collection" /> : undefined}>
              <table className="data">
                <thead><tr><th>Title</th><th>Type</th><th>Status</th><th>Version</th><th>Language</th><th>Added</th><th /></tr></thead>
                <tbody>
                  {docs.data?.map((d) => (
                    <tr key={d.id}>
                      <td><a href={`/documents/${d.id}`} onClick={(e) => { e.preventDefault(); nav(`/documents/${d.id}`); }}>{d.title}</a><div className="small muted">{d.filename}</div></td>
                      <td>{d.doc_type}</td>
                      <td>
                        <Badge tone={statusTone(d.status)}>{d.status}</Badge>
                        {(d.status === "pending" || d.status === "processing") && <> <Spinner label="Processing" /></>}
                        {d.status === "failed" && <button className="btn small ghost" onClick={() => void showError(d.id)}>why?</button>}
                      </td>
                      <td>v{d.current_version}</td><td>{d.language ?? "–"}</td><td className="muted small">{relTime(d.created_at)}</td>
                      <td style={{ textAlign: "right" }}>{canWrite && <button className="btn small danger" onClick={() => void remove(d.id, d.title)} aria-label={`Delete ${d.title}`}>Delete</button>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Loading>
          </div>
        </div>
      </div>
      {modal === "collection" && <NewCollection onClose={() => setModal(null)} onCreated={(id) => { setModal(null); cols.reload(); setSel(id); }} />}
      {modal === "access" && sel && <AccessModal collectionId={sel} onClose={() => setModal(null)} />}
    </>
  );
}

function NewCollection({ onClose, onCreated }: { onClose: () => void; onCreated: (id: string) => void }) {
  const api = useApi();
  const [name, setName] = useState("");
  const [restricted, setRestricted] = useState(false);
  const [error, setError] = useState<unknown>();
  return (
    <Modal title="New collection" onClose={onClose}>
      <ErrorBanner error={error} />
      <div className="field"><label htmlFor="cname">Name</label><input id="cname" value={name} onChange={(e) => setName(e.target.value)} /></div>
      <label className="row"><input type="checkbox" checked={restricted} onChange={(e) => setRestricted(e.target.checked)} /> Restricted (only people you grant access can see it)</label>
      <div className="row" style={{ marginTop: 14 }}>
        <button className="btn primary" disabled={!name.trim()} onClick={() => api.createCollection(name.trim(), restricted).then((c) => onCreated(c.id), setError)}>Create</button>
        <button className="btn" onClick={onClose}>Cancel</button>
      </div>
    </Modal>
  );
}

function AccessModal({ collectionId, onClose }: { collectionId: string; onClose: () => void }) {
  const api = useApi();
  const users = useAsync(() => api.adminUsers(), [api]);
  const [granted, setGranted] = useState<string[]>([]);
  const [error, setError] = useState<unknown>();
  return (
    <Modal title="Grant access to this collection" onClose={onClose}>
      <ErrorBanner error={error} />
      <Loading loading={users.loading} error={users.error}>
        {users.data?.filter((u) => u.role !== "admin").map((u) => (
          <div key={u.id} className="row" style={{ marginBottom: 6 }}>
            <span className="grow">{u.email} <Badge>{u.role}</Badge></span>
            <button className="btn small" disabled={granted.includes(u.id)} onClick={() => api.grantAccess(collectionId, u.id).then(() => setGranted((g) => [...g, u.id]), setError)}>{granted.includes(u.id) ? "Granted" : "Grant"}</button>
          </div>
        ))}
        <p className="small muted">Admins can always see every collection.</p>
      </Loading>
      <button className="btn" onClick={onClose}>Done</button>
    </Modal>
  );
}
