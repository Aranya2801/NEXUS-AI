import { BarChart, type Bar } from "../components/BarChart";
import { Badge, EmptyState, Loading, PageHead } from "../components/ui";
import { useApi } from "../features/auth/AuthContext";
import { useAsync } from "../hooks/useAsync";
import { ApiError } from "../services/api";
import type { EvaluationResults } from "../types/schemas";

const GROUPS: { key: "retrieval" | "generation" | "engineering"; title: string }[] = [
  { key: "retrieval", title: "Retrieval" }, { key: "generation", title: "Generation" }, { key: "engineering", title: "Engineering" },
];

/** Builds one bar set per metric across systems. Optional `<metric>_ci_low` / `<metric>_ci_high` keys draw confidence intervals. */
export function metricBars(res: EvaluationResults, group: "retrieval" | "generation" | "engineering"): { metric: string; bars: Bar[] }[] {
  const metrics = new Set<string>();
  for (const s of res.systems) for (const k of Object.keys(s[group] ?? {})) if (!/_ci_(low|high)$/.test(k)) metrics.add(k);
  return [...metrics].map((metric) => ({
    metric,
    bars: res.systems.flatMap((s) => {
      const v = s[group]?.[metric];
      if (v === undefined) return [];
      const lo = s[group]?.[`${metric}_ci_low`], hi = s[group]?.[`${metric}_ci_high`];
      return [{ label: s.name, value: v, ...(lo !== undefined && hi !== undefined ? { low: lo, high: hi } : {}) }];
    }),
  }));
}

export function EvaluationPage() {
  const api = useApi();
  const res = useAsync(async () => {
    try { return await api.evaluation(); } catch (e) { if (e instanceof ApiError && e.status === 404) return null; throw e; }
  }, [api]);
  return (
    <>
      <PageHead title="Evaluation analytics" />
      <Loading loading={res.loading && res.data === undefined} error={res.error} empty={res.data === null ? (
        <div className="card"><EmptyState title="No evaluation results yet" hint="Benchmark results appear here once an evaluation run has produced them. Nothing is shown until real experiments exist; no numbers are made up." /></div>
      ) : undefined}>
        {res.data && (
          <div className="col" style={{ gap: 14 }}>
            <div className="card">
              <div className="row wrap"><h2 className="grow">{res.data.dataset.name}</h2>
                <Badge>{res.data.dataset.n_questions} questions</Badge>
                {res.data.dataset.synthetic && <Badge tone="warn" title="Questions were written over a synthetic corpus">synthetic dataset</Badge>}
                {res.data.environment.mock && <Badge tone="warn" title="Mock LLM and/or hashing embeddings: validates plumbing, not real model quality">mock components</Badge>}
                <Badge>{res.data.environment.llm}</Badge><Badge>{res.data.environment.embedder}</Badge></div>
              <div className="small muted">Generated {new Date(res.data.generated_at).toLocaleString()}</div>
              {res.data.notes.map((n, i) => <div key={i} className="small" style={{ marginTop: 4 }}>• {n}</div>)}
            </div>
            {GROUPS.map((g) => {
              const sets = metricBars(res.data!, g.key);
              return sets.length === 0 ? null : (
                <div key={g.key} className="card"><h2>{g.title}</h2>
                  <div className="grid cols-2" style={{ marginTop: 10 }}>{sets.map((s) => <BarChart key={s.metric} title={s.metric.replace(/_/g, " ")} bars={s.bars} format={(v) => (Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(3))} />)}</div>
                </div>
              );
            })}
          </div>
        )}
      </Loading>
    </>
  );
}
