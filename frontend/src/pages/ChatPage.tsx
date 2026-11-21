import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { AgentTimeline } from "../components/AgentTimeline";
import { AnswerView } from "../components/AnswerView";
import { Badge, EmptyState, ErrorBanner, Spinner } from "../components/ui";
import { useApi } from "../features/auth/AuthContext";
import { EvidencePanel } from "../features/chat/EvidencePanel";
import { messageFromServer, type ChatMsg } from "../features/chat/chatState";
import { ReviewBanner } from "../features/chat/ReviewBanner";
import { useChatStream } from "../features/chat/useChatStream";
import { useAsync } from "../hooks/useAsync";
import { answerTone, pct, relTime, truncate } from "../services/format";

function Feedback({ runId }: { runId: string }) {
  const api = useApi();
  const [rating, setRating] = useState<number | null>(null);
  useEffect(() => { api.myFeedback(runId).then((f) => setRating(f.rating), () => undefined); }, [api, runId]);
  const rate = (r: 1 | -1) => api.feedback(runId, r).then(() => setRating(r), () => undefined);
  return (
    <div className="row small" role="group" aria-label="Rate this answer">
      <span className="muted">Helpful?</span>
      <button className="btn small" aria-pressed={rating === 1} style={rating === 1 ? { borderColor: "var(--good)" } : undefined} onClick={() => void rate(1)} aria-label="Helpful">👍</button>
      <button className="btn small" aria-pressed={rating === -1} style={rating === -1 ? { borderColor: "var(--bad)" } : undefined} onClick={() => void rate(-1)} aria-label="Not helpful">👎</button>
    </div>
  );
}

export function ChatPage() {
  const api = useApi();
  const nav = useNavigate();
  const { conversationId: urlConv } = useParams();
  const prompt = (useLocation().state as { prompt?: string } | null)?.prompt;
  const chat = useChatStream(api);
  const threads = useAsync(() => api.conversations(), [api]);
  const collections = useAsync(() => api.collections(), [api]);
  const contexts = useAsync(() => api.contexts(), [api]);
  const [input, setInput] = useState("");
  const [scope, setScope] = useState("");
  const [context, setContext] = useState("");
  const [alwaysReview, setAlwaysReview] = useState(false);
  const [active, setActive] = useState<number | null>(null);
  const [focusId, setFocusId] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<unknown>();
  const endRef = useRef<HTMLDivElement>(null);
  const autoSent = useRef(false);

  useEffect(() => {
    setLoadError(undefined);
    if (!urlConv) { chat.reset(null); return; }
    api.messages(urlConv).then((ms) => chat.reset(urlConv, ms.map(messageFromServer)), setLoadError);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlConv, api]);

  useEffect(() => { endRef.current?.scrollIntoView?.({ behavior: "smooth" }); }, [chat.messages.length, chat.messages.at(-1)?.text]);

  async function submit(text = input) {
    const t = text.trim();
    if (!t || chat.busy) return;
    setInput("");
    setActive(null);
    const id = await chat.send(t, { collection_ids: scope ? [scope] : null, research_context_id: context || null, review: alwaysReview ? "always" : "auto" });
    setFocusId(id);
    threads.reload();
  }

  useEffect(() => { if (prompt && !autoSent.current) { autoSent.current = true; nav(".", { replace: true, state: null }); void submit(prompt); } /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [prompt]);

  const onKey = (e: KeyboardEvent) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void submit(); } };
  const lastAssistant = [...chat.messages].reverse().find((m) => m.role === "assistant");
  const focused: ChatMsg | undefined = chat.messages.find((m) => m.id === focusId) ?? lastAssistant;

  async function decide(m: ChatMsg, d: "approve" | "edit" | "reject", answer?: string, note?: string) {
    if (!m.runId) return;
    await chat.resume(m.id, m.runId, d, answer, note);
    threads.reload();
  }

  return (
    <div className="chat">
      <section className="threads" aria-label="Conversations">
        <div style={{ padding: 10 }}><Link className="btn primary" style={{ display: "block", textAlign: "center" }} to="/chat">New research</Link></div>
        {threads.data?.map((t) => (
          <Link key={t.id} to={`/chat/${t.id}`} className={t.id === (urlConv ?? chat.conversationId) ? "active" : ""}>
            {truncate(t.title, 44)}<div className="small muted">{relTime(t.updated_at)}</div>
          </Link>
        ))}
      </section>

      <section style={{ display: "flex", flexDirection: "column" }}>
        <div className="convo" style={{ flex: 1 }}>
          <ErrorBanner error={loadError} />
          {chat.messages.length === 0 && <EmptyState title="Ask across your documents" hint="Answers are built only from evidence you can access, verified claim by claim, and cited with page numbers." />}
          {chat.messages.map((m) => m.role === "user" ? (
            <div key={m.id} className="msg user">{m.text}</div>
          ) : (
            <article key={m.id} className="msg assistant" onClick={() => setFocusId(m.id)} aria-label="Answer" data-testid="assistant-message">
              <div className="row wrap">
                {m.result?.answer_status && <Badge tone={answerTone(m.result.answer_status)}>{m.result.answer_status.replace(/_/g, " ")}</Badge>}
                {m.result?.query_class && <Badge tone="info">{m.result.query_class}</Badge>}
                {m.result && <Badge title="Heuristic, uncalibrated retrieval confidence">evidence {pct(m.result.confidence.score)}</Badge>}
                {m.result && m.result.verification.unsupported ? <Badge tone="bad">{m.result.verification.unsupported} claim(s) removed</Badge> : null}
                {m.streaming && <span className="row small muted"><Spinner /> {m.evidenceCount !== undefined ? `${m.evidenceCount} passages retrieved` : "working"}</span>}
              </div>
              <AgentTimeline trace={m.trace} running={!!m.streaming} reviewPending={m.reviewPending} />
              {m.error && <div className="banner" role="alert">{m.error}</div>}
              {(m.text || m.streaming) && <AnswerView text={m.text} citationIds={m.citations.map((c) => c.id)} activeId={active} onSelect={(id) => { setActive(id); setFocusId(m.id); }} streaming={m.streaming && !m.result} />}
              {m.reviewPending && m.result && (
                <ReviewBanner answer={m.result.answer} reasons={((m.result.review as { reasons?: string[] } | null | undefined)?.reasons) ?? []} busy={chat.busy} onDecide={(d, a, n) => void decide(m, d, a, n)} />
              )}
              {m.result && !m.reviewPending && !m.streaming && <Feedback runId={m.result.run_id} />}
            </article>
          ))}
          <div ref={endRef} />
        </div>
        <div className="composer">
          <div className="row wrap small" style={{ marginBottom: 6 }}>
            <select aria-label="Collection scope" value={scope} onChange={(e) => setScope(e.target.value)} style={{ width: "auto" }}>
              <option value="">All accessible collections</option>{collections.data?.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
            <select aria-label="Research context" value={context} onChange={(e) => setContext(e.target.value)} style={{ width: "auto" }}>
              <option value="">No research context</option>{contexts.data?.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
            <label className="row" style={{ margin: 0 }}><input type="checkbox" checked={alwaysReview} onChange={(e) => setAlwaysReview(e.target.checked)} /> Require human review</label>
          </div>
          <div className="row" style={{ alignItems: "flex-end" }}>
            <textarea className="grow" aria-label="Your question" value={input} onChange={(e) => setInput(e.target.value)} onKeyDown={onKey} placeholder="Ask a research question…  (Enter to send, Shift+Enter for a new line)" rows={2} />
            {chat.busy ? <button className="btn danger" onClick={chat.abort}>Stop</button> : <button className="btn primary" onClick={() => void submit()} disabled={!input.trim()}>Send</button>}
          </div>
        </div>
      </section>

      <section aria-label="Evidence"><EvidencePanel msg={focused} activeCitation={active} onSelectCitation={setActive} /></section>
    </div>
  );
}
