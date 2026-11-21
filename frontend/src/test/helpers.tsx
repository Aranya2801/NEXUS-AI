import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { vi } from "vitest";
import { AuthProvider } from "../features/auth/AuthContext";
import type { ApiClient } from "../services/api";
import type { ChatResult, Citation, User } from "../types/schemas";

export const USER: User = { id: "u1", org_id: "o1", email: "ana@example.com", full_name: "Ana", role: "admin", department: null };

/** A strict fake: any API method the test did not stub throws, so accidental/unexpected calls fail loudly. */
export function fakeApi(overrides: Record<string, unknown> = {}): ApiClient {
  const calls = new Map<string, unknown>();
  return new Proxy({}, {
    get(_t, prop: string) {
      if (prop in overrides) return overrides[prop];
      if (prop === "then") return undefined;
      if (!calls.has(prop)) calls.set(prop, vi.fn(() => Promise.reject(new Error(`unmocked api.${prop}()`))));
      return calls.get(prop);
    },
    set(_t, prop: string, value) {   // lets tests stub methods after creation (e.g. `me` for the signed-in user)
      overrides[prop] = value;
      return true;
    },
  }) as ApiClient;
}

export function renderWithApp(ui: ReactElement, opts: { api?: ApiClient; user?: User | null; route?: string; path?: string } = {}) {
  const user = opts.user === undefined ? USER : opts.user;
  const api = opts.api ?? fakeApi();
  if (user) {
    sessionStorage.setItem("nexus.token", "test-token");
    if (!("me" in (api as unknown as Record<string, unknown>) && vi.isMockFunction((api as unknown as Record<string, unknown>)["me"]) && opts.api)) (api as unknown as Record<string, unknown>)["me"] = vi.fn().mockResolvedValue(user);
  }
  const wrapped = (
    <MemoryRouter initialEntries={[opts.route ?? "/"]}>
      <AuthProvider client={api}>
        {opts.path ? <Routes><Route path={opts.path} element={ui} /><Route path="*" element={<div>other page</div>} /></Routes> : ui}
      </AuthProvider>
    </MemoryRouter>
  );
  return { api, ...render(wrapped) };
}

export const citation = (over: Partial<Citation> = {}): Citation => ({
  id: 1, chunk_id: "c1", document_id: "d1", title: "Longformer paper", filename: "longformer.pdf", page_start: 3, page_end: 3, section: "2. METHODS",
  version: 1, is_current: true, quote: "Longformer extends Transformer with sparse attention patterns.", quotes: ["Longformer extends Transformer with sparse attention patterns."], ...over,
});

export function chatResult(over: Partial<ChatResult> = {}): ChatResult {
  return {
    run_id: "r1", status: "completed", conversation_id: "cv1", answer: "Longformer extends Transformer with sparse attention patterns. [1]", answer_status: "grounded",
    citations: [citation()],
    verification: { supported: 1, weak: 0, unsupported: 1, claims_total: 2, claims_kept: 1, citation_coverage: 1, judge_used: true,
      claims: [{ index: 0, text: "Longformer extends Transformer with sparse attention patterns.", status: "supported", reason: "ok", coverage: 1 },
               { index: 1, text: "Longformer reached 99.9 BLEU.", status: "unsupported", reason: "number_not_in_sources", coverage: 0.8 }] },
    quality: { relevance: 0.8, completeness: 0.7, groundedness: 0.5, citation_coverage: 1, evidence_confidence: 0.6, reasons: [] },
    analysis: {}, query_class: "factoid", intent: "answer_question", plan: { name: "factoid" }, queries: ["What does Longformer do?"],
    confidence: { score: 0.62, coverage: 0.8, top_score: 0.7, margin: 0.2 },
    evidence: [{ chunk_id: "c1", document_id: "d1", title: "Longformer paper", page_start: 3, section: "2. METHODS", version: 1, score: 0.91, scores: { rerank: 0.91 }, ranks: { dense: 1, sparse: 2 }, snippet: "Longformer extends Transformer…" }],
    graph: null, injection_sanitized: 0, review: null, errors: [],
    trace: [{ agent: "orchestrator", node: "classify", ms: 2 }, { agent: "retrieval", node: "retrieve_branch", ms: 10 }, { agent: "verification", node: "verify_facts", ms: 3 }],
    llm_usage: { calls: 2, prompt_tokens: 100, completion_tokens: 20, by_task: {} }, ...over,
  };
}
