import { useNavigate } from "react-router-dom";
import { Badge, EmptyState, Loading, PageHead } from "../components/ui";
import { useApi } from "../features/auth/AuthContext";
import { useAsync } from "../hooks/useAsync";
import { relTime, statusTone, truncate } from "../services/format";

export function RunsPage() {
  const api = useApi();
  const nav = useNavigate();
  const runs = useAsync(() => api.runs(), [api]);
  return (
    <>
      <PageHead title="Agent runs"><button className="btn" onClick={runs.reload}>Refresh</button></PageHead>
      <div className="card flush">
        <Loading loading={runs.loading && !runs.data} error={runs.error} empty={runs.data && !runs.data.length ? <EmptyState title="No runs yet" hint="Every research question runs the multi-agent workflow; you can inspect each run here." /> : undefined}>
          <table className="data"><thead><tr><th>Question</th><th>Status</th><th>Started</th></tr></thead><tbody>
            {runs.data?.map((r) => (
              <tr key={r.run_id} className="clickable" onClick={() => nav(`/runs/${r.run_id}`)}><td>{truncate(r.query, 90)}</td><td><Badge tone={statusTone(r.status)}>{r.status}</Badge></td><td className="muted small">{relTime(r.created_at)}</td></tr>
            ))}
          </tbody></table>
        </Loading>
      </div>
    </>
  );
}
