import { useState } from "react";
import { Badge, EmptyState, ErrorBanner, Loading, PageHead } from "../components/ui";
import { useApi } from "../features/auth/AuthContext";
import { useAsync } from "../hooks/useAsync";
import { relTime, statusTone } from "../services/format";
import type { Connector } from "../types/schemas";

const KIND_LABEL: Record<string, string> = { google_drive: "Google Drive", sharepoint: "SharePoint", slack: "Slack", teams: "Microsoft Teams", sql: "SQL database", rest: "REST API" };

export function ConnectorsPage() {
  const api = useApi();
  const list = useAsync(() => api.connectors(), [api]);
  const [error, setError] = useState<unknown>();
  const [open, setOpen] = useState<string>();
  const items = useAsync(() => (open ? api.connectorItems(open) : Promise.resolve([])), [api, open]);

  async function sync(c: Connector, reset: boolean) {
    try { await api.syncConnector(c.id, reset); setTimeout(list.reload, 800); } catch (e) { setError(e); }
  }

  return (
    <>
      <PageHead title="Data connectors"><button className="btn" onClick={list.reload}>Refresh</button></PageHead>
      <ErrorBanner error={error ?? list.error} />
      <p className="muted small">Connectors are created through the API (credentials are write-only and stored encrypted). Strict ACL mode only ingests items whose source permissions cover everyone who can see the target collection.</p>
      <Loading loading={list.loading && !list.data} error={undefined} empty={list.data && !list.data.length ? <EmptyState title="No connectors" hint="POST /connectors to add Google Drive, SharePoint, Slack, Teams, SQL or REST sources." /> : undefined}>
        {list.data?.map((c) => (
          <div className="card" key={c.id} style={{ marginBottom: 12 }} data-connector={c.id}>
            <div className="row wrap" style={{ gap: 8 }}>
              <strong>{c.name}</strong><Badge>{KIND_LABEL[c.kind] ?? c.kind}</Badge><Badge tone={c.acl_mode === "strict" ? "good" : "warn"}>ACL: {c.acl_mode}</Badge>
              {!c.enabled && <Badge tone="warn">disabled</Badge>}
              {c.last_run && <Badge tone={statusTone(c.last_run.status)}>{c.last_run.status}</Badge>}
              <span className="muted small">{c.last_sync_at ? `last sync ${relTime(c.last_sync_at)}` : "never synced"}</span>
              <span style={{ flex: 1 }} />
              <button className="btn small" onClick={() => void sync(c, false)} disabled={!c.enabled}>Sync now</button>
              <button className="btn small" onClick={() => void sync(c, true)} disabled={!c.enabled} title="Forget the watermark and re-scan everything">Full resync</button>
              <button className="btn small" onClick={() => setOpen(open === c.id ? undefined : c.id)}>{open === c.id ? "Hide items" : "Items"}</button>
            </div>
            {c.last_run && <div className="small muted" style={{ marginTop: 6 }}>{Object.entries(c.last_run.stats).map(([k, v]) => `${k}: ${v}`).join(" · ")}{c.last_run.error && <span className="error"> · {c.last_run.error}</span>}</div>}
            {open === c.id && (
              <table style={{ marginTop: 8 }}><thead><tr><th>Item</th><th>Status</th><th>Detail</th></tr></thead>
                <tbody>{items.data?.map((i) => <tr key={i.external_id}><td>{i.name}</td><td><Badge tone={i.status === "synced" ? "good" : i.status === "failed" ? "bad" : "warn"}>{i.status}</Badge></td><td className="small muted">{i.detail}</td></tr>)}</tbody></table>
            )}
          </div>
        ))}
      </Loading>
    </>
  );
}
