import { useState, type FormEvent } from "react";
import { Badge, EmptyState, ErrorBanner, Loading, PageHead, Stat, Tabs } from "../components/ui";
import { useApi } from "../features/auth/AuthContext";
import { useAsync } from "../hooks/useAsync";
import { relTime, statusTone, truncate } from "../services/format";

type Tab = "overview" | "users" | "audit" | "jobs";
const STATUS_ROWS: [string, string][] = [["documents", "Documents"], ["ingestion_jobs", "Ingestion jobs"], ["agent_runs", "Agent runs"], ["reports", "Reports"]];

export function AdminPage() {
  const api = useApi();
  const [tab, setTab] = useState<Tab>("overview");
  const [error, setError] = useState<unknown>();
  const ov = useAsync(() => api.adminOverview(), [api]);
  const users = useAsync(() => api.adminUsers(), [api]);
  const [action, setAction] = useState("");
  const [offset, setOffset] = useState(0);
  const audit = useAsync(() => api.audit(action || undefined, 25, offset), [api, action, offset]);
  const [jobStatus, setJobStatus] = useState("");
  const jobs = useAsync(() => api.adminJobs(jobStatus || undefined), [api, jobStatus]);
  const [nu, setNu] = useState({ email: "", password: "", role: "viewer" });

  const patch = (id: string, p: { role?: string; is_active?: boolean }) => api.updateUser(id, p).then(users.reload, setError);
  async function addUser(e: FormEvent) {
    e.preventDefault();
    try { await api.createUser(nu); setNu({ email: "", password: "", role: "viewer" }); users.reload(); } catch (err) { setError(err); }
  }

  return (
    <>
      <PageHead title="Administration"><button className="btn" onClick={() => { ov.reload(); users.reload(); audit.reload(); jobs.reload(); }}>Refresh</button></PageHead>
      <ErrorBanner error={error} />
      <Tabs<Tab> active={tab} onChange={setTab} tabs={[{ id: "overview", label: "Overview" }, { id: "users", label: "Users" }, { id: "audit", label: "Audit log" }, { id: "jobs", label: "Ingestion jobs" }]} />

      {tab === "overview" && (
        <Loading loading={ov.loading && !ov.data} error={ov.error}>
          {ov.data && (
            <div className="col" style={{ gap: 14 }}>
              <div className="grid cols-4">
                <div className="card"><Stat label="Users" value={ov.data.counts.users} /></div><div className="card"><Stat label="Collections" value={ov.data.counts.collections} /></div>
                <div className="card"><Stat label="Indexed chunks" value={ov.data.counts.chunks} /></div><div className="card"><Stat label="Conversations" value={ov.data.counts.conversations} /></div>
                <div className="card"><Stat label="Graph entities" value={ov.data.graph.entities} hint={`${ov.data.graph.relations} relations`} /></div>
                <div className="card"><Stat label="Answer feedback" value={`👍 ${ov.data.feedback.up} · 👎 ${ov.data.feedback.down}`} /></div>
              </div>
              <div className="grid cols-2">
                <div className="card"><h2>Status breakdown</h2>
                  {STATUS_ROWS.map(([k, label]) => { const m = ov.data![k as "documents"]; return (
                    <div key={k} className="row wrap" style={{ margin: "8px 0" }}><span style={{ width: 120 }}>{label}</span>{Object.keys(m).length === 0 ? <span className="muted small">none</span> : Object.entries(m).map(([s, n]) => <Badge key={s} tone={statusTone(s)}>{s}: {n}</Badge>)}</div>); })}
                </div>
                <div className="card"><h2>Runtime configuration</h2>
                  <table className="data"><tbody>{Object.entries(ov.data.runtime).map(([k, v]) => <tr key={k}><td className="muted">{k.replace(/_/g, " ")}</td><td className="mono">{String(v)}</td></tr>)}</tbody></table>
                  {(ov.data.runtime.llm === "mock-llm" || ov.data.runtime.embedder.startsWith("hashing")) && <div className="banner warn" style={{ marginTop: 10 }}>Offline mock components are active: good for demos and tests, not representative of real answer quality.</div>}
                </div>
              </div>
            </div>
          )}
        </Loading>
      )}

      {tab === "users" && (
        <div className="col" style={{ gap: 14 }}>
          <form className="card row wrap" onSubmit={(e) => void addUser(e)} aria-label="Add user">
            <input style={{ width: 220 }} type="email" placeholder="email" value={nu.email} onChange={(e) => setNu({ ...nu, email: e.target.value })} aria-label="New user email" required />
            <input style={{ width: 200 }} type="password" placeholder="temporary password (10+)" value={nu.password} onChange={(e) => setNu({ ...nu, password: e.target.value })} aria-label="New user password" required minLength={10} />
            <select style={{ width: 120 }} value={nu.role} onChange={(e) => setNu({ ...nu, role: e.target.value })} aria-label="New user role"><option>viewer</option><option>editor</option><option>admin</option></select>
            <button className="btn primary" type="submit">Add user</button>
          </form>
          <div className="card flush"><Loading loading={users.loading && !users.data} error={users.error}>
            <table className="data"><thead><tr><th>User</th><th>Role</th><th>Active</th></tr></thead><tbody>
              {users.data?.map((u) => (
                <tr key={u.id}><td>{u.email}<div className="small muted">{u.full_name}</div></td>
                  <td><select style={{ width: 120 }} value={u.role} onChange={(e) => void patch(u.id, { role: e.target.value })} aria-label={`Role for ${u.email}`}><option>viewer</option><option>editor</option><option>admin</option></select></td>
                  <td><label className="row" style={{ margin: 0 }}><input type="checkbox" checked={u.is_active} onChange={(e) => void patch(u.id, { is_active: e.target.checked })} aria-label={`Active: ${u.email}`} /> {u.is_active ? "active" : "disabled"}</label></td></tr>
              ))}
            </tbody></table></Loading></div>
          <div className="small muted">Role changes and deactivation take effect immediately, even for sessions that are already signed in.</div>
        </div>
      )}

      {tab === "audit" && (
        <div className="card flush">
          <div className="row" style={{ padding: 10 }}>
            <select style={{ width: 220 }} value={action} onChange={(e) => { setAction(e.target.value); setOffset(0); }} aria-label="Filter by action">
              <option value="">All actions</option>{["auth.login", "auth.login_failed", "auth.register", "user.update", "document.upload", "document.delete", "chat.run", "chat.review", "report.create", "report.export", "memory.forget", "feedback.give"].map((a) => <option key={a}>{a}</option>)}
            </select>
            <span className="right row"><button className="btn small" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 25))}>Newer</button><button className="btn small" disabled={(audit.data?.length ?? 0) < 25} onClick={() => setOffset(offset + 25)}>Older</button></span>
          </div>
          <Loading loading={audit.loading && !audit.data} error={audit.error} empty={audit.data && !audit.data.length ? <EmptyState title="No matching entries" /> : undefined}>
            <table className="data"><thead><tr><th>When</th><th>Action</th><th>Resource</th><th>Detail</th></tr></thead><tbody>
              {audit.data?.map((a) => <tr key={a.id}><td className="small muted">{relTime(a.created_at)}</td><td><Badge tone={a.action.includes("failed") ? "bad" : "neutral"}>{a.action}</Badge></td><td className="mono small">{a.resource ? truncate(a.resource, 12) : "–"}</td><td className="mono small">{truncate(JSON.stringify(a.detail), 70)}</td></tr>)}
            </tbody></table>
          </Loading>
        </div>
      )}

      {tab === "jobs" && (
        <div className="card flush">
          <div className="row" style={{ padding: 10 }}><select style={{ width: 180 }} value={jobStatus} onChange={(e) => setJobStatus(e.target.value)} aria-label="Filter by job status"><option value="">All statuses</option>{["queued", "running", "succeeded", "failed", "dead"].map((s) => <option key={s}>{s}</option>)}</select></div>
          <Loading loading={jobs.loading && !jobs.data} error={jobs.error} empty={jobs.data && !jobs.data.length ? <EmptyState title="No jobs" /> : undefined}>
            <table className="data"><thead><tr><th>Document</th><th>Status</th><th>Stage</th><th>Attempts</th><th>Error</th></tr></thead><tbody>
              {jobs.data?.map((j) => <tr key={j.id}><td>{j.title}</td><td><Badge tone={statusTone(j.status)}>{j.status}</Badge></td><td>{j.stage}</td><td>{j.attempts}/{j.max_attempts}</td><td className="small" style={{ color: "var(--bad)" }}>{j.error ? truncate(j.error, 80) : ""}</td></tr>)}
            </tbody></table>
          </Loading>
        </div>
      )}
    </>
  );
}
