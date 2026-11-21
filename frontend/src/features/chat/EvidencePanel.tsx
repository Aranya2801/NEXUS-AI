import { useState } from "react";
import { Link } from "react-router-dom";
import { Badge, EmptyState, Json, Meter, Tabs } from "../../components/ui";
import { ms, pageLabel, pct } from "../../services/format";
import type { ChatMsg } from "./chatState";

type Tab = "citations" | "evidence" | "verification" | "analysis" | "run";

export function EvidencePanel({ msg, activeCitation, onSelectCitation }: { msg?: ChatMsg; activeCitation: number | null; onSelectCitation: (id: number | null) => void }) {
  const [tab, setTab] = useState<Tab>("citations");
  if (!msg) return <EmptyState title="Evidence appears here" hint="Ask a question: citations, retrieved passages, verification and the agent run show up beside the answer." />;
  const r = msg.result;
  return (
    <div style={{ padding: 12 }}>
      <Tabs<Tab> active={tab} onChange={setTab} tabs={[
        { id: "citations", label: `Citations (${msg.citations.length})` }, { id: "evidence", label: `Evidence${r ? ` (${r.evidence.length})` : ""}` },
        { id: "verification", label: "Verification" }, { id: "analysis", label: "Analysis" }, { id: "run", label: "Run" },
      ]} />
      {tab === "citations" && (
        msg.citations.length === 0 ? <EmptyState title="No citations" hint="An answer without verified evidence has nothing to cite." /> :
        msg.citations.map((c) => (
          <div key={c.id} className={`cite-card${activeCitation === c.id ? " active" : ""}`} id={`cite-${c.id}`} onClick={() => onSelectCitation(c.id)}>
            <div className="row"><span className="chip active" style={{ cursor: "default" }}>{c.id}</span><b>{c.title}</b></div>
            <div className="small muted">
              {c.filename} {pageLabel(c.page_start, c.page_end) && <>· {pageLabel(c.page_start, c.page_end)} </>}{c.section && <>· {c.section} </>}{c.version !== undefined && <>· v{c.version} </>}
              {c.is_current === false && <Badge tone="warn">superseded version</Badge>}
            </div>
            {(c.quotes?.length ? c.quotes : c.quote ? [c.quote] : []).map((q, i) => <div key={i} className="quote">“{q}”</div>)}
            {c.document_id && <div style={{ marginTop: 6 }}><Link className="small" to={`/documents/${c.document_id}${c.chunk_id ? `?chunk=${c.chunk_id}` : ""}`}>Open in source viewer →</Link></div>}
          </div>
        ))
      )}
      {tab === "evidence" && (!r ? <div className="muted small">Available when the run finishes.</div> : r.evidence.length === 0 ? <EmptyState title="Nothing retrieved" hint="No accessible passage matched this question." /> :
        r.evidence.map((e) => (
          <div key={e.chunk_id} className="cite-card">
            <div className="row"><b className="grow">{e.title}</b><span className="mono small">{e.score.toFixed(3)}</span></div>
            <div className="small muted">{pageLabel(e.page_start)} {e.section && `· ${e.section}`} · v{e.version}</div>
            <div className="row wrap" style={{ margin: "4px 0" }}>{Object.entries(e.ranks).map(([k, v]) => <Badge key={k} tone={k === "graph" ? "info" : "neutral"}>{k} #{v}</Badge>)}{e.scores["rerank"] !== undefined && <Badge tone="good">rerank {e.scores["rerank"].toFixed(2)}</Badge>}</div>
            <div className="small">{e.snippet}</div>
            <Link className="small" to={`/documents/${e.document_id}?chunk=${e.chunk_id}`}>Open →</Link>
          </div>
        )))}
      {tab === "verification" && (!r ? <div className="muted small">Available when the run finishes.</div> : (
        <div className="col">
          <div className="row wrap">
            <Badge tone="good">{r.verification.supported ?? 0} supported</Badge><Badge tone="warn">{r.verification.weak ?? 0} weak</Badge><Badge tone="bad">{r.verification.unsupported ?? 0} removed</Badge>
            <Badge title="Share of claims that cite an existing passage">citation coverage {pct(r.verification.citation_coverage)}</Badge>
            <Badge tone={r.verification.judge_used ? "info" : "neutral"}>{r.verification.judge_used ? "LLM judge used" : "lexical check only"}</Badge>
            {(r.injection_sanitized ?? 0) > 0 && <Badge tone="warn" title="Instruction-like sentences removed from retrieved documents before generation">{r.injection_sanitized} injection-like passage(s) sanitised</Badge>}
          </div>
          <table className="data"><thead><tr><th>Claim</th><th>Status</th></tr></thead><tbody>
            {(r.verification.claims ?? []).map((c) => (
              <tr key={c.index}><td>{c.text}<div className="small muted">coverage {pct(c.coverage)} · {c.reason}</div></td>
                <td><Badge tone={c.status === "supported" ? "good" : c.status === "weak" ? "warn" : "bad"}>{c.status}</Badge></td></tr>
            ))}
          </tbody></table>
          <div className="small muted">Unsupported claims are removed from the answer; weakly supported ones are marked “limited support”.</div>
        </div>
      ))}
      {tab === "analysis" && (!r ? <div className="muted small">Available when the run finishes.</div> : <Analysis analysis={r.analysis} />)}
      {tab === "run" && (!r ? <div className="muted small">Available when the run finishes.</div> : (
        <div className="col">
          <div className="row wrap"><Badge tone="info">{r.query_class}</Badge><Badge>{r.intent}</Badge><Badge tone={r.status === "completed" ? "good" : "warn"}>{r.status}</Badge></div>
          <div><span className="small muted">Evidence confidence</span><Meter value={r.confidence.score} label="Evidence confidence" /><span className="small mono">{pct(r.confidence.score)} (coverage {pct(r.confidence.coverage)}, top {pct(r.confidence.top_score)})</span></div>
          {(["relevance", "completeness", "groundedness"] as const).map((k) => <div key={k}><span className="small muted">{k}</span><Meter value={r.quality[k]} label={k} /></div>)}
          {r.quality.reasons?.length ? <div className="small">Review reasons: {r.quality.reasons.join(", ")}</div> : null}
          <div className="small muted">Queries: {r.queries.join(" · ")}</div>
          <div className="small">LLM calls {r.llm_usage.calls ?? 0} · tokens {(r.llm_usage.prompt_tokens ?? 0) + (r.llm_usage.completion_tokens ?? 0)} · {ms(r.trace.reduce((s, t) => s + t.ms, 0))} in agents</div>
          {r.errors.length > 0 && <div className="banner">{r.errors.join("; ")}</div>}
          <Link to={`/runs/${r.run_id}`}>Inspect the full agent run →</Link>
          <details><summary className="small">Plan</summary><Json value={r.plan} /></details>
        </div>
      ))}
    </div>
  );
}

function Analysis({ analysis }: { analysis: Record<string, unknown> }) {
  const contradictions = (analysis["contradictions"] ?? []) as { type: string; a: { sentence: string }; b: { sentence: string } }[];
  const findings = (analysis["quantitative_findings"] ?? []) as { sentence: string; numbers: string[] }[];
  const docs = (analysis["documents"] ?? []) as { title: string; key_sentence: string }[];
  if (!contradictions.length && !findings.length && !docs.length) return <EmptyState title="No analysis" hint="Comparison and contradiction analysis runs when there is evidence from several documents." />;
  return (
    <div className="col">
      {contradictions.length > 0 && <div className="banner warn">Possible contradictions (heuristic candidates — verify in the sources)</div>}
      {contradictions.map((c, i) => <div key={i} className="cite-card"><Badge tone="warn">{c.type}</Badge><div className="quote">“{c.a.sentence}”</div><div className="quote">“{c.b.sentence}”</div></div>)}
      {findings.length > 0 && <><h3>Quantitative findings</h3>{findings.map((f, i) => <div key={i} className="small">• {f.sentence} <span className="mono">{f.numbers.join(", ")}</span></div>)}</>}
      {docs.length > 1 && <><h3>Per-document key point</h3>{docs.map((d, i) => <div key={i} className="small"><b>{d.title}:</b> {d.key_sentence}</div>)}</>}
    </div>
  );
}
