import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "../services/api";
import type { EvaluationResults, StreamEvent } from "../types/schemas";
import { chatResult, citation, fakeApi, renderWithApp, USER } from "../test/helpers";
import { AdminPage } from "./AdminPage";
import { aggregateCitations, CitationExplorerPage } from "./CitationExplorerPage";
import { ChatPage } from "./ChatPage";
import { ConnectorsPage } from "./ConnectorsPage";
import { AgentPage } from "./AgentPage";
import { DocumentsPage, validateFile } from "./DocumentsPage";
import { DocumentViewerPage, groupByPage } from "./DocumentViewerPage";
import { EvaluationPage, metricBars } from "./EvaluationPage";
import { KnowledgeExplorerPage } from "./KnowledgeExplorerPage";
import { LoginPage } from "./LoginPage";
import { ReportViewPage } from "./ReportViewPage";
import { SettingsPage } from "./SettingsPage";
import { RequireAuth } from "../features/auth/RequireAuth";

const doc = (over = {}) => ({ id: "d1", collection_id: "c1", title: "Alpha paper", filename: "alpha.pdf", doc_type: "pdf", department: null, language: "en", status: "ready", current_version: 1, created_at: new Date().toISOString(), ...over });

// ------------------------------------------------------------------------------------------------ auth
describe("authentication", () => {
  it("signs in, then leaves the login page", async () => {
    const login = vi.fn().mockResolvedValue({ access_token: "t", token_type: "bearer", expires_in: 3600 });
    const me = vi.fn().mockResolvedValue(USER);
    const { container } = renderWithApp(<LoginPage />, { user: null, api: fakeApi({ login, me }), route: "/login", path: "/login" });
    await userEvent.type(screen.getByLabelText("Email"), "ana@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "correct-horse-battery");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(screen.getByText("other page")).toBeInTheDocument());
    expect(login).toHaveBeenCalledWith("ana@example.com", "correct-horse-battery");
    expect(sessionStorage.getItem("nexus.token")).toBe("t");
    expect(container).toBeTruthy();
  });

  it("shows server errors and blocks too-short passwords on registration before calling the API", async () => {
    const login = vi.fn().mockRejectedValue(new ApiError(401, "authentication_failed", "Incorrect email or password"));
    const register = vi.fn();
    renderWithApp(<LoginPage />, { user: null, api: fakeApi({ login, register }), route: "/login", path: "/login" });
    await userEvent.type(screen.getByLabelText("Email"), "a@b.co");
    await userEvent.type(screen.getByLabelText("Password"), "wrong-password");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Incorrect email or password");
    await userEvent.click(screen.getByRole("tab", { name: "Create organization" }));
    await userEvent.type(screen.getByLabelText("Organization name"), "Acme");
    await userEvent.clear(screen.getByLabelText("Password"));
    await userEvent.type(screen.getByLabelText("Password"), "short");
    await userEvent.click(screen.getByRole("button", { name: "Create organization" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("at least 10 characters");
    expect(register).not.toHaveBeenCalled();
  });

  it("redirects signed-out visitors to the login page and keeps non-admins out of admin pages", async () => {
    renderWithApp(<RequireAuth><div>secret</div></RequireAuth>, { user: null, route: "/private", path: "/private" });
    await waitFor(() => expect(screen.queryByText("secret")).not.toBeInTheDocument());
    renderWithApp(<RequireAuth role="admin"><div>admin only</div></RequireAuth>, { user: { ...USER, role: "viewer" } });
    expect(await screen.findByText("Administrators only")).toBeInTheDocument();
    expect(screen.queryByText("admin only")).not.toBeInTheDocument();
  });

  it("signs the user out when the API rejects the token", async () => {
    const me = vi.fn().mockRejectedValue(new ApiError(401, "authentication_failed", "expired"));
    sessionStorage.setItem("nexus.token", "stale");
    renderWithApp(<RequireAuth><div>secret</div></RequireAuth>, { user: null, api: fakeApi({ me }), route: "/x", path: "/x" });
    await waitFor(() => expect(sessionStorage.getItem("nexus.token")).toBeNull());
  });
});

// ------------------------------------------------------------------------------------------------ documents
describe("documents", () => {
  it("validates files before upload", () => {
    expect(validateFile({ name: "a.pdf", size: 10 })).toBeNull();
    expect(validateFile({ name: "run.exe", size: 10 })).toMatch(/not supported/);
    expect(validateFile({ name: "empty.txt", size: 0 })).toMatch(/empty/);
    expect(validateFile({ name: "big.pdf", size: 26 * 1024 * 1024 })).toMatch(/larger than 25 MB/);
    expect(validateFile({ name: "noext", size: 5 })).toMatch(/not supported/);
  });

  it("uploads valid files, rejects invalid ones client-side and reports duplicates", async () => {
    const uploadDocument = vi.fn().mockResolvedValueOnce({ outcome: "created", document: doc(), job: null }).mockResolvedValueOnce({ outcome: "duplicate", document: doc(), job: null });
    const api = fakeApi({ collections: vi.fn().mockResolvedValue([{ id: "c1", name: "Papers", description: "", is_restricted: false }]), documents: vi.fn().mockResolvedValue([doc()]), uploadDocument });
    renderWithApp(<DocumentsPage />, { api, user: { ...USER, role: "editor" } });
    const input = await screen.findByLabelText("Choose files");
    // applyAccept:false simulates drag-and-drop, which (unlike the file picker) bypasses the input's accept filter
    await userEvent.setup({ applyAccept: false }).upload(input, [new File(["x"], "new.txt"), new File(["x"], "again.txt"), new File(["MZ"], "bad.exe")]);
    expect(await screen.findByText(/new.txt: Uploaded, processing/)).toBeInTheDocument();
    expect(screen.getByText(/again.txt: Already uploaded/)).toBeInTheDocument();
    expect(screen.getByText(/bad.exe: file type ".exe" is not supported/)).toBeInTheDocument();
    expect(uploadDocument).toHaveBeenCalledTimes(2);                       // the .exe never reached the server
  });

  it("hides upload and delete controls from viewers", async () => {
    const api = fakeApi({ collections: vi.fn().mockResolvedValue([{ id: "c1", name: "Papers", description: "", is_restricted: true }]), documents: vi.fn().mockResolvedValue([doc()]) });
    renderWithApp(<DocumentsPage />, { api, user: { ...USER, role: "viewer" } });
    expect(await screen.findByText("Alpha paper")).toBeInTheDocument();
    expect(screen.queryByLabelText("Choose files")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Delete/ })).not.toBeInTheDocument();
    expect(screen.getByText("restricted")).toBeInTheDocument();
  });

  it("asks before deleting and only then calls the API", async () => {
    const deleteDocument = vi.fn().mockResolvedValue(undefined);
    const api = fakeApi({ collections: vi.fn().mockResolvedValue([{ id: "c1", name: "P", description: "", is_restricted: false }]), documents: vi.fn().mockResolvedValue([doc()]), deleteDocument });
    renderWithApp(<DocumentsPage />, { api });
    const del = await screen.findByRole("button", { name: "Delete Alpha paper" });
    vi.spyOn(window, "confirm").mockReturnValueOnce(false);
    await userEvent.click(del);
    expect(deleteDocument).not.toHaveBeenCalled();
    vi.spyOn(window, "confirm").mockReturnValueOnce(true);
    await userEvent.click(del);
    await waitFor(() => expect(deleteDocument).toHaveBeenCalledWith("d1"));
  });

  it("source viewer groups passages by page and highlights the cited chunk", async () => {
    const mk = (id: string, page: number | null, ord: number) => ({ id, level: "child", kind: "text", ordinal: ord, text: `text ${id}`, page_start: page, page_end: page, section: "1. INTRO", char_start: 0, char_end: 1, token_count: 3, version: 1, is_current: true });
    const chunks = [mk("a", 1, 0), mk("b", 1, 1), mk("c", 2, 2)];
    expect(groupByPage(chunks).map((g) => [g.page, g.chunks.length])).toEqual([[1, 2], [2, 1]]);
    const api = fakeApi({ document: vi.fn().mockResolvedValue(doc()), chunks: vi.fn().mockResolvedValue(chunks), contexts: vi.fn().mockResolvedValue([]) });
    renderWithApp(<DocumentViewerPage />, { api, route: "/documents/d1?chunk=c", path: "/documents/:id" });
    expect(await screen.findByText("Page 2")).toBeInTheDocument();
    expect(document.querySelector('[data-chunk="c"]')).toHaveClass("highlight");
    expect(document.querySelector('[data-chunk="a"]')).not.toHaveClass("highlight");
    expect(Element.prototype.scrollIntoView).toHaveBeenCalled();
  });
});

// ------------------------------------------------------------------------------------------------ chat
function streamingApi(events: StreamEvent[], extra: Record<string, unknown> = {}) {
  const tick = () => new Promise<void>((r) => setTimeout(r, 0));   // yield between events like a real network stream (no nested act())
  const streamChat = vi.fn(async (_req: unknown, onEvent: (e: StreamEvent) => void) => { for (const e of events) { onEvent(e); await tick(); } });
  return fakeApi({ conversations: vi.fn().mockResolvedValue([]), collections: vi.fn().mockResolvedValue([]), contexts: vi.fn().mockResolvedValue([]), streamChat, ...extra });
}

describe("research chat", () => {
  it("streams agent progress, then shows the verified answer with clickable citations and evidence", async () => {
    const result = chatResult();
    const api = streamingApi([
      { type: "run", run_id: "r1", conversation_id: "cv1" },
      { type: "agent", node: "classify", agent: "orchestrator", ms: 1, detail: {} },
      { type: "evidence", count: 4, documents: 2 },
      { type: "delta", text: "Longformer extends " },
      { type: "done", result },
    ], { myFeedback: vi.fn().mockResolvedValue({ run_id: "r1", rating: null, comment: "" }), feedback: vi.fn().mockResolvedValue({ run_id: "r1", rating: 1, comment: "" }) });
    renderWithApp(<ChatPage />, { api, route: "/chat", path: "/chat" });
    await userEvent.type(screen.getByLabelText("Your question"), "What does Longformer do?");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    const msg = await screen.findByTestId("assistant-message");
    expect(await within(msg).findByText("grounded")).toBeInTheDocument();                // answer-status badge (appears when the final result arrives)
    expect(within(msg).getByText(/Longformer extends Transformer with sparse attention patterns\./)).toBeInTheDocument();
    expect(within(msg).getByText("1 claim(s) removed")).toBeInTheDocument();             // verification is visible, not hidden
    expect(document.querySelector('[data-agent="orchestrator"]')).toHaveAttribute("data-state", "done");

    await userEvent.click(within(msg).getByRole("button", { name: "Citation 1" }));      // chip opens the evidence
    const panel = screen.getByRole("region", { name: "Evidence" }) ?? document.body;
    expect(within(panel).getByText(/p\.3/)).toBeInTheDocument();
    expect(within(panel).getByText(/“Longformer extends Transformer with sparse attention patterns\.”/)).toBeInTheDocument();
    expect(within(panel).getByRole("link", { name: /Open in source viewer/ })).toHaveAttribute("href", "/documents/d1?chunk=c1");

    await userEvent.click(within(panel).getByRole("tab", { name: "Verification" }));
    expect(within(panel).getByText("Longformer reached 99.9 BLEU.")).toBeInTheDocument();
    expect(within(panel).getByText("unsupported")).toBeInTheDocument();
    expect(within(panel).getByText(/number_not_in_sources/)).toBeInTheDocument();

    await userEvent.click(within(msg).getByRole("button", { name: "Helpful" }));
    await waitFor(() => expect((api as unknown as { feedback: ReturnType<typeof vi.fn> }).feedback).toHaveBeenCalledWith("r1", 1));
  });

  it("sends the chosen scope, research context and review preference", async () => {
    const api = streamingApi([{ type: "done", result: chatResult() }], {
      collections: vi.fn().mockResolvedValue([{ id: "c1", name: "Papers", description: "", is_restricted: false }]),
      contexts: vi.fn().mockResolvedValue([{ id: "x1", name: "Long context", goal: "", pinned_documents: 1 }]),
      myFeedback: vi.fn().mockResolvedValue({ run_id: "r1", rating: null, comment: "" }),
    });
    renderWithApp(<ChatPage />, { api, route: "/chat", path: "/chat" });
    await userEvent.selectOptions(await screen.findByLabelText("Collection scope"), "c1");
    await userEvent.selectOptions(screen.getByLabelText("Research context"), "x1");
    await userEvent.click(screen.getByLabelText(/Require human review/));
    await userEvent.type(screen.getByLabelText("Your question"), "q{Enter}");
    const call = (api as unknown as { streamChat: ReturnType<typeof vi.fn> }).streamChat.mock.calls[0]![0];
    expect(call).toMatchObject({ message: "q", collection_ids: ["c1"], research_context_id: "x1", review: "always" });
  });

  it("shows stream failures as an alert without losing the question", async () => {
    const api = streamingApi([{ type: "error", message: "The research workflow failed" }]);
    renderWithApp(<ChatPage />, { api, route: "/chat", path: "/chat" });
    await userEvent.type(screen.getByLabelText("Your question"), "hello{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent("The research workflow failed");
    expect(screen.getByText("hello")).toBeInTheDocument();
  });

  it("pauses for human review and resumes with the reviewer's decision", async () => {
    const paused = chatResult({ status: "needs_review", review: { pending: true, reasons: ["review_mode_always"] } });
    const approved = chatResult({ status: "completed" });
    const streamResume = vi.fn(async (_id: string, _d: string, onEvent: (e: StreamEvent) => void) => { onEvent({ type: "done", result: approved }); });
    const api = streamingApi([{ type: "run", run_id: "r1", conversation_id: "cv1" }, { type: "review_required" }, { type: "done", result: paused }],
      { streamResume, myFeedback: vi.fn().mockResolvedValue({ run_id: "r1", rating: null, comment: "" }) });
    renderWithApp(<ChatPage />, { api, route: "/chat", path: "/chat" });
    await userEvent.type(screen.getByLabelText("Your question"), "q{Enter}");
    const banner = await screen.findByRole("region", { name: "Human review required" });
    expect(banner).toHaveTextContent("review_mode_always");
    expect(banner).toHaveTextContent("Nothing is saved to memory until the answer is approved");
    expect(screen.queryByRole("group", { name: "Rate this answer" })).not.toBeInTheDocument();       // no rating while pending
    await userEvent.click(within(banner).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(streamResume).toHaveBeenCalledWith("r1", "approve", expect.any(Function), undefined, undefined, expect.anything()));
    await waitFor(() => expect(screen.queryByRole("region", { name: "Human review required" })).not.toBeInTheDocument());
    expect(await screen.findByRole("group", { name: "Rate this answer" })).toBeInTheDocument();
  });

  it("loads a saved conversation with its stored citations", async () => {
    const api = fakeApi({
      conversations: vi.fn().mockResolvedValue([{ id: "cv1", title: "Earlier", updated_at: new Date().toISOString() }]), collections: vi.fn().mockResolvedValue([]), contexts: vi.fn().mockResolvedValue([]),
      messages: vi.fn().mockResolvedValue([{ id: "m1", role: "user", content: "Q?", citations: [], verification: {}, created_at: "x" },
        { id: "m2", role: "assistant", content: "Stored answer [1]", citations: [citation()], verification: {}, created_at: "x" }]),
    });
    renderWithApp(<ChatPage />, { api, route: "/chat/cv1", path: "/chat/:conversationId" });
    expect(await screen.findByText(/Stored answer/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Citation 1" })).toBeInTheDocument();
    expect(screen.getByText("Longformer paper")).toBeInTheDocument();
  });
});

// ------------------------------------------------------------------------------------------------ other pages
describe("citations, graph, reports, evaluation", () => {
  it("aggregates stored citations per document", () => {
    const msgs = [
      { id: "1", role: "assistant", content: "", verification: {}, created_at: "", citations: [citation({ page_start: 3 }), citation({ id: 2, chunk_id: "c2", page_start: 1, quote: "Another quote." })] },
      { id: "2", role: "assistant", content: "", verification: {}, created_at: "", citations: [citation({ document_id: "d2", title: "Other" })] },
      { id: "3", role: "user", content: "", verification: {}, created_at: "", citations: [citation()] },
    ];
    const agg = aggregateCitations(msgs);
    expect(agg[0]).toMatchObject({ document_id: "d1", count: 2, pages: [1, 3] });
    expect(agg).toHaveLength(2);
  });

  it("citation explorer lists documents and expands their verbatim quotes", async () => {
    const api = fakeApi({ conversations: vi.fn().mockResolvedValue([{ id: "cv1", title: "t", updated_at: "x" }]),
      messages: vi.fn().mockResolvedValue([{ id: "m", role: "assistant", content: "", verification: {}, created_at: "", citations: [citation()] }]) });
    renderWithApp(<CitationExplorerPage />, { api });
    await userEvent.click(await screen.findByText("Longformer paper"));
    expect(screen.getByText(/“Longformer extends Transformer with sparse attention patterns\.”/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "open →" })).toHaveAttribute("href", "/documents/d1?chunk=c1");
  });

  it("knowledge explorer shows seeds, relations and their supporting sentences", async () => {
    const graphQuery = vi.fn().mockResolvedValue({
      seeds: [{ id: "a", name: "Transformer", type: "concept" }], documents: 2,
      nodes: { a: { id: "a", name: "Transformer", type: "concept", depth: 0 }, b: { id: "b", name: "Longformer", depth: 1 } },
      edges: [{ source_id: "b", predicate: "extends", target_id: "a", confidence: 0.7, depth: 1, chunk_id: "c1", document_id: "d1", title: "Longformer paper", page: 2, evidence: "Longformer extends Transformer." }], hits: [],
    });
    renderWithApp(<KnowledgeExplorerPage />, { api: fakeApi({ entities: vi.fn().mockResolvedValue([]), graphQuery }) });
    await userEvent.type(screen.getByLabelText("Graph query"), "Transformer improvements");
    await userEvent.click(screen.getByRole("button", { name: "Explore" }));
    expect(await screen.findByText(/1 relations/)).toBeInTheDocument();
    expect(graphQuery).toHaveBeenCalledWith("Transformer improvements", 2);
    expect(screen.getByText(/“Longformer extends Transformer\.”/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Longformer paper p\.2/ })).toHaveAttribute("href", "/documents/d1?chunk=c1");
  });

  it("report view renders cited sections, flags mock mode and downloads exports", async () => {
    const content = {
      title: "T", topic: "transformers", generated_at: new Date().toISOString(), redacted: true,
      documents: [{ document_id: "d1", title: "Alpha", filename: "alpha.pdf", versions: [1], pages: [1, 2] }],
      executive_summary: [{ text: "Alpha uses sparse attention.", citations: [1] }],
      sections: [{ id: "m", title: "Methodologies", blocks: [{ type: "claim", text: "Alpha uses sparse attention.", citations: [1] }, { type: "table", columns: ["Doc", "Result", "Numbers", "Page"], rows: [{ cells: ["Alpha", "perplexity of 18.3", "18.3", "2"], citations: [1] }] }, { type: "note", text: "Heuristic." }] }],
      citations: [{ id: 1, title: "Alpha", filename: "alpha.pdf", page_start: 1, page_end: 1, version: 1, quote: "Alpha uses sparse attention.", quotes: ["Alpha uses sparse attention."], document_id: "d1", chunk_id: "c1" }],
      verification: { claims_total: 2, claims_kept: 1, unsupported_removed: [1], dropped_at_final_check: [] }, coverage_notes: ["1 document(s) analysed."],
      method: { statement: "Verified.", llm: "mock-llm", reranker: "lexical-v1", warning: "Generated in offline mock mode" },
    };
    const exportReport = vi.fn().mockResolvedValue({ blob: new Blob(["%PDF"]), filename: "t.pdf" });
    const api = fakeApi({ report: vi.fn().mockResolvedValue({ id: "r1", title: "T", topic: "transformers", status: "ready", progress: [], error: null, content, created_at: "x", updated_at: "x" }), exportReport });
    const createUrl = vi.fn().mockReturnValue("blob:x");
    Object.assign(URL, { createObjectURL: createUrl, revokeObjectURL: vi.fn() });
    renderWithApp(<ReportViewPage />, { api, route: "/reports/r1", path: "/reports/:id" });
    expect(await screen.findByText("Generated in offline mock mode")).toBeInTheDocument();
    expect(screen.getByText(/Some sources are no longer accessible/)).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "Source 1" })[0]).toHaveAttribute("href", "#src-1");
    expect(screen.getByRole("columnheader", { name: "Result" })).toBeInTheDocument();
    expect(screen.getByText("[1] Alpha")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Export PDF" }));
    await waitFor(() => expect(exportReport).toHaveBeenCalledWith("r1", "pdf"));
    expect(createUrl).toHaveBeenCalled();
  });

  it("evaluation page shows an honest empty state on 404 and renders real results with their caveats", async () => {
    const res: EvaluationResults = {
      generated_at: new Date().toISOString(), dataset: { name: "bench", n_questions: 100, synthetic: true }, environment: { llm: "mock-llm", embedder: "hashing-v1:64", mock: true },
      systems: [{ id: "s1", name: "Dense", retrieval: { ndcg_at_10: 0.4, ndcg_at_10_ci_low: 0.3, ndcg_at_10_ci_high: 0.5 }, engineering: { latency_mean_ms: 12 } }, { id: "s2", name: "Hybrid", retrieval: { ndcg_at_10: 0.6 }, engineering: { latency_mean_ms: 20 } }], notes: ["Synthetic corpus"],
    };
    expect(metricBars(res, "retrieval")).toEqual([{ metric: "ndcg_at_10", bars: [{ label: "Dense", value: 0.4, low: 0.3, high: 0.5 }, { label: "Hybrid", value: 0.6 }] }]);
    const { unmount } = renderWithApp(<EvaluationPage />, { api: fakeApi({ evaluation: vi.fn().mockRejectedValue(new ApiError(404, "not_found", "Not found")) }) });
    expect(await screen.findByText("No evaluation results yet")).toBeInTheDocument();
    unmount();
    renderWithApp(<EvaluationPage />, { api: fakeApi({ evaluation: vi.fn().mockResolvedValue(res) }) });
    expect(await screen.findByText("synthetic dataset")).toBeInTheDocument();
    expect(screen.getByText("mock components")).toBeInTheDocument();
    expect(screen.getByRole("figure", { name: "ndcg at 10" })).toBeInTheDocument();
  });
});

// ------------------------------------------------------------------------------------------------ settings & admin
describe("settings and administration", () => {
  const settingsApi = (over: Record<string, unknown> = {}) => fakeApi({
    memory: vi.fn().mockResolvedValue([{ id: "m1", kind: "long_term", scope_id: "", key: "style", value: "concise", expires_at: null, updated_at: "x" }]),
    contexts: vi.fn().mockResolvedValue([]), recentDocuments: vi.fn().mockResolvedValue([]), ...over,
  });

  it("requires confirmation before erasing memory and conversations", async () => {
    const forget = vi.fn().mockResolvedValue({ deleted_items: 3, deleted_conversations: 2 });
    renderWithApp(<SettingsPage />, { api: settingsApi({ forget }) });
    await userEvent.click(await screen.findByRole("tab", { name: "All memory" }));
    await userEvent.click(screen.getByRole("button", { name: "Delete everything…" }));
    expect(forget).not.toHaveBeenCalled();
    vi.spyOn(window, "confirm").mockReturnValueOnce(true);
    await userEvent.click(screen.getByRole("button", { name: "Delete everything…" }));
    await waitFor(() => expect(forget).toHaveBeenCalledWith(undefined, true));
    expect(await screen.findByText(/Deleted 3 memory item\(s\) and 2 conversation\(s\)/)).toBeInTheDocument();
  });

  it("saves preferences and shows hidden findings without their content", async () => {
    const setPreference = vi.fn().mockResolvedValue({});
    const api = settingsApi({ setPreference, contexts: vi.fn().mockResolvedValue([{ id: "x1", name: "Ctx", goal: "", pinned_documents: 1 }]),
      context: vi.fn().mockResolvedValue({ id: "x1", name: "Ctx", goal: "", pinned_documents: ["d1"], findings: [{ id: "f1", hidden: true, reason: "source no longer accessible" }, { id: "f2", hidden: false, title: "Alpha", quote: "A quote", page_start: 2, document_id: "d1", chunk_id: "c1" }] }) });
    renderWithApp(<SettingsPage />, { api });
    expect(await screen.findByText("concise")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Preference key"), "tone");
    await userEvent.type(screen.getByLabelText("Preference value"), "formal");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(setPreference).toHaveBeenCalledWith("tone", "formal"));
    await userEvent.click(screen.getByRole("tab", { name: "Research contexts" }));
    await userEvent.click(await screen.findByRole("button", { name: /Ctx/ }));
    expect(await screen.findByText(/A saved finding is hidden: source no longer accessible/)).toBeInTheDocument();
    expect(screen.getByText("A quote")).toBeInTheDocument();
  });

  it("admin changes a role and surfaces the last-admin guard error", async () => {
    const updateUser = vi.fn().mockRejectedValue(new ApiError(409, "conflict", "An organisation must keep at least one active admin"));
    const api = fakeApi({
      adminOverview: vi.fn().mockResolvedValue({ counts: { users: 1, collections: 1, chunks: 9, conversations: 2 }, documents: { ready: 3 }, ingestion_jobs: { dead: 1 }, agent_runs: {}, reports: {}, graph: { entities: 4, relations: 2, mentions: 6 }, feedback: { up: 2, down: 1 }, runtime: { env: "test", llm: "mock-llm", embedder: "hashing-v1:64", reranker: "lexical-v1", graph_extractor: "rule-v1", human_review_mode: "auto", max_retrieval_loops: 2 } }),
      adminUsers: vi.fn().mockResolvedValue([{ id: "u1", email: "ana@example.com", full_name: "Ana", role: "admin", department: null, is_active: true }]),
      audit: vi.fn().mockResolvedValue([]), adminJobs: vi.fn().mockResolvedValue([]), updateUser,
    });
    renderWithApp(<AdminPage />, { api });
    expect(await screen.findByText(/Offline mock components are active/)).toBeInTheDocument();
    expect(screen.getByText("dead: 1")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "Users" }));
    await userEvent.selectOptions(await screen.findByLabelText("Role for ana@example.com"), "viewer");
    expect(await screen.findByRole("alert")).toHaveTextContent("at least one active admin");
    expect(updateUser).toHaveBeenCalledWith("u1", { role: "viewer" });
  });
});

// ------------------------------------------------------------------------------------------------ multimodal
describe("multimodal documents", () => {
  it("accepts image uploads and rejects SVG (script risk)", () => {
    expect(validateFile(new File(["x"], "chart.png"))).toBeNull();
    expect(validateFile(new File(["x"], "scan.TIFF"))).toBeNull();
    expect(validateFile(new File(["x"], "logo.svg"))).toMatch(/not supported/);
  });

  it("shows figures with OCR status and a kind badge on figure chunks", async () => {
    const chunk = { id: "f1", level: "child", kind: "figure", ordinal: 0, text: "Figure 1: bar chart", page_start: 2, page_end: 2, section: "Figure 1 (page 2)", char_start: 0, char_end: 5, token_count: 4, version: 1, is_current: true };
    const fig = { index: 1, page: 2, source: "pdf", width: 100, height: 50, url: "/documents/d1/figures/1", ocr: { status: "ok", chars: 12, confidence: 0.9 }, caption: { status: "ok", model: "mock-vision" } };
    const api = fakeApi({ document: vi.fn().mockResolvedValue(doc()), chunks: vi.fn().mockResolvedValue([chunk]), contexts: vi.fn().mockResolvedValue([]),
      figures: vi.fn().mockResolvedValue([fig]), figureBlobUrl: vi.fn().mockResolvedValue("blob:fake") });
    renderWithApp(<DocumentViewerPage />, { api, route: "/documents/d1", path: "/documents/:id" });
    expect(await screen.findByText(/Figure 1 · page 2 · OCR ok · caption ok/)).toBeInTheDocument();
    expect(await screen.findByAltText("Figure 1, page 2")).toHaveAttribute("src", "blob:fake");
    expect(screen.getByText("figure")).toBeInTheDocument();
  });
});

// ------------------------------------------------------------------------------------------------ connectors
describe("data connectors page", () => {
  it("lists connectors with ACL mode and last run, syncs, and shows skipped items with their reason", async () => {
    const c = { id: "k1", name: "Eng drive", kind: "google_drive", collection_id: "c1", acl_mode: "strict", enabled: true, interval_minutes: null, last_sync_at: new Date().toISOString(), has_cursor: true,
      last_run: { id: "r1", status: "partial", stats: { ingested: 2, skipped_acl: 1 }, error: null, started_at: new Date().toISOString(), finished_at: new Date().toISOString() } };
    const syncConnector = vi.fn().mockResolvedValue({ status: "accepted" });
    const api = fakeApi({ connectors: vi.fn().mockResolvedValue([c]), syncConnector,
      connectorItems: vi.fn().mockResolvedValue([{ external_id: "x", name: "narrow", status: "skipped_acl", detail: "1 NEXUS user(s) cannot read it at the source", document_id: null }]) });
    renderWithApp(<ConnectorsPage />, { api, route: "/connectors", path: "/connectors", user: { ...USER, role: "admin" } });
    expect(await screen.findByText("Eng drive")).toBeInTheDocument();
    expect(screen.getByText("Google Drive")).toBeInTheDocument();
    expect(screen.getByText("ACL: strict")).toBeInTheDocument();
    expect(screen.getByText(/ingested: 2 · skipped_acl: 1/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Sync now" }));
    expect(syncConnector).toHaveBeenCalledWith("k1", false);
    await userEvent.click(screen.getByRole("button", { name: "Full resync" }));
    expect(syncConnector).toHaveBeenCalledWith("k1", true);
    await userEvent.click(screen.getByRole("button", { name: "Items" }));
    expect(await screen.findByText(/cannot read it at the source/)).toBeInTheDocument();
  });
});

// ------------------------------------------------------------------------------------------------ autonomous agent
describe("autonomous agent page", () => {
  it("runs a goal and shows the verified answer, budget and the per-step trace including failures", async () => {
    const run = { id: "a1", goal: "g", status: "completed", error: null, result: {
      answer: "Attention relates tokens [1]. Computed by tools (not from documents): 2 * 21 = 42", answer_status: "grounded",
      citations: [{ id: 1, title: "Attention", filename: "a.txt", page_start: null, quote: "Self-attention relates tokens" }],
      steps: [
        { id: "a", wave: 0, tool: "search_documents", purpose: "find", args: { query: "attention" }, depends_on: [], ok: true, error: null, ms: 12, summary: "Attention: ..." },
        { id: "r", wave: 1, tool: "read_document", purpose: "deeper", args: { document_id: "zzz" }, depends_on: ["a"], ok: false, error: "document not found", ms: 3, summary: "" }],
      planner: ["llm", "rules"], stopped_reason: "planner_done", budget: { steps_used: 2, max_steps: 12, waves_used: 2, max_waves: 3 }, computations: [{ tool: "calculator", result: 42 }] } };
    const agenticRun = vi.fn().mockResolvedValue(run);
    renderWithApp(<AgentPage />, { api: fakeApi({ agenticRun }), route: "/agent", path: "/agent" });
    expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Research goal"), "What does attention do?");
    await userEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByText(/Computed by tools \(not from documents\)/)).toBeInTheDocument();
    expect(agenticRun).toHaveBeenCalledWith("What does attention do?");
    expect(screen.getByText("grounded")).toBeInTheDocument();
    expect(screen.getByText("2/12 steps · 2/3 waves")).toBeInTheDocument();
    expect(screen.getByText("document not found")).toBeInTheDocument();
    expect(document.querySelectorAll("[data-step]")).toHaveLength(2);
  });
});
