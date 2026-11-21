import { chatResult } from "../../test/helpers";
import { chatReducer, messageFromServer, type ChatMsg } from "./chatState";

const start = (): ChatMsg[] => chatReducer(chatReducer([], { type: "user", id: "u", text: "q" }), { type: "assistant_start", id: "a" });
const ev = (state: ChatMsg[], event: Parameters<typeof chatReducer>[1] extends infer A ? A extends { type: "event"; event: infer E } ? E : never : never) => chatReducer(state, { type: "event", id: "a", event });

describe("chatReducer", () => {
  it("accumulates streamed agents, evidence and text, then the final result replaces them", () => {
    let s = start();
    s = ev(s, { type: "run", run_id: "r1", conversation_id: "cv" });
    s = ev(s, { type: "agent", node: "classify", agent: "orchestrator", ms: 1, detail: {} });
    s = ev(s, { type: "evidence", count: 5, documents: 2 });
    s = ev(s, { type: "delta", text: "Hello " }); s = ev(s, { type: "delta", text: "world" });
    expect(s[1]).toMatchObject({ runId: "r1", text: "Hello world", evidenceCount: 5, streaming: true, trace: [{ agent: "orchestrator", node: "classify" }] });
    const result = chatResult({ answer: "FINAL [1]" });
    s = ev(s, { type: "done", result });
    expect(s[1]).toMatchObject({ streaming: false, text: "FINAL [1]", citations: result.citations, reviewPending: false });
    expect(s[1]?.trace.map((t) => t.node)).toEqual(["classify", "retrieve_branch", "verify_facts"]);   // authoritative trace from the result
  });
  it("marks review-pending runs and keeps them resumable", () => {
    let s = ev(start(), { type: "review_required" });
    expect(s[1]?.reviewPending).toBe(true);
    s = ev(s, { type: "done", result: chatResult({ status: "needs_review" }) });
    expect(s[1]).toMatchObject({ reviewPending: true, runId: "r1" });
    s = ev(s, { type: "done", result: chatResult({ status: "completed" }) });
    expect(s[1]?.reviewPending).toBe(false);
  });
  it("records stream errors and failures without losing the partial answer", () => {
    let s = ev(ev(start(), { type: "delta", text: "partial" }), { type: "error", message: "boom" });
    expect(s[1]).toMatchObject({ streaming: false, error: "boom", text: "partial" });
    s = chatReducer(start(), { type: "fail", id: "a", message: "network" });
    expect(s[1]?.error).toBe("network");
    expect(chatReducer(start(), { type: "end", id: "a" })[1]?.streaming).toBe(false);
  });
  it("loads persisted messages", () => {
    const m = messageFromServer({ id: "m1", role: "assistant", content: "Answer [1]", citations: [{ id: 1, title: "T" }], verification: {}, created_at: "x" });
    expect(chatReducer([], { type: "load", messages: [m] })[0]).toMatchObject({ role: "assistant", text: "Answer [1]", citations: [{ id: 1, title: "T" }] });
  });
});
