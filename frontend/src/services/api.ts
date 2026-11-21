import { z } from "zod";
import { SseParser } from "./sse";
import * as S from "../types/schemas";
import type { StreamEvent } from "../types/schemas";

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public details?: unknown) {
    super(message);
    this.name = "ApiError";
  }
}

export interface ChatRequest {
  message: string;
  conversation_id?: string | null;
  collection_ids?: string[] | null;
  review?: "auto" | "always";
  research_context_id?: string | null;
}

type Query = Record<string, string | number | boolean | null | undefined>;
interface Opts<T> {
  body?: unknown;
  query?: Query;
  schema?: z.ZodType<T>;
  signal?: AbortSignal;
}

export const MAX_UPLOAD_MB = 25;
export const ALLOWED_EXTENSIONS = [".pdf", ".docx", ".pptx", ".xlsx", ".txt", ".md", ".html", ".htm", ".csv",
  ".png", ".jpg", ".jpeg", ".webp", ".gif", ".tif", ".tiff", ".bmp"];

export class ApiClient {
  constructor(
    private baseUrl: string,
    private getToken: () => string | null,
    private onUnauthorized: () => void = () => {},
    private fetchImpl: typeof fetch = (...a) => fetch(...a),
  ) {}

  private url(path: string, query?: Query): string {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(query ?? {})) if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
    const s = qs.toString();
    return `${this.baseUrl}${path}${s ? `?${s}` : ""}`;
  }

  private headers(json: boolean): Record<string, string> {
    const h: Record<string, string> = {};
    const t = this.getToken();
    if (t) h["Authorization"] = `Bearer ${t}`;
    if (json) h["Content-Type"] = "application/json";
    return h;
  }

  private async fail(res: Response): Promise<never> {
    let code = "http_error";
    let message = `Request failed (${res.status})`;
    let details: unknown;
    try {
      const body = (await res.json()) as { error?: { code?: string; message?: string; details?: unknown }; detail?: unknown };
      if (body.error) {
        code = body.error.code ?? code;
        message = body.error.message ?? message;
        details = body.error.details;
      } else if (Array.isArray(body.detail)) {
        code = "validation_failed";
        message = body.detail.map((d: { loc?: unknown[]; msg?: string }) => `${(d.loc ?? []).slice(1).join(".")}: ${d.msg}`).join("; ");
      }
    } catch {
      /* non-JSON error body */
    }
    if (res.status === 401) this.onUnauthorized();
    throw new ApiError(res.status, code, message, details);
  }

  async request<T = unknown>(method: string, path: string, opts: Opts<T> = {}): Promise<T> {
    const res = await this.fetchImpl(this.url(path, opts.query), {
      method,
      headers: this.headers(opts.body !== undefined),
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      signal: opts.signal,
    });
    if (!res.ok) await this.fail(res);
    if (res.status === 204) return undefined as T;
    const data: unknown = await res.json();
    return opts.schema ? opts.schema.parse(data) : (data as T);
  }

  get = <T>(path: string, schema: z.ZodType<T>, query?: Query, signal?: AbortSignal) => this.request<T>("GET", path, { schema, query, signal });
  post = <T>(path: string, schema: z.ZodType<T>, body?: unknown) => this.request<T>("POST", path, { schema, body: body ?? {} });

  // ---- auth
  login = (email: string, password: string) => this.post("/auth/login", S.TokenSchema, { email, password });
  register = (organization_name: string, email: string, password: string, full_name = "") =>
    this.post("/auth/register", S.UserSchema, { organization_name, email, password, full_name });
  me = () => this.get("/auth/me", S.UserSchema);
  createUser = (b: { email: string; password: string; role: string; full_name?: string }) => this.post("/auth/users", S.UserSchema, b);

  // ---- knowledge base
  collections = () => this.get("/collections", z.array(S.CollectionSchema));
  createCollection = (name: string, is_restricted: boolean, description = "") => this.post("/collections", S.CollectionSchema, { name, is_restricted, description });
  grantAccess = (collectionId: string, user_id: string) => this.request<void>("POST", `/collections/${collectionId}/access`, { body: { user_id } });
  documents = (collection_id?: string, limit = 200) => this.get("/documents", z.array(S.DocumentSchema), { collection_id, limit });
  document = (id: string) => this.get(`/documents/${id}`, S.DocumentSchema);
  documentJob = (id: string) => this.get(`/documents/${id}/job`, S.JobSchema);
  chunks = (id: string, level: "child" | "parent" = "child", limit = 500, offset = 0) => this.get(`/documents/${id}/chunks`, z.array(S.ChunkSchema), { level, limit, offset });
  deleteDocument = (id: string) => this.request<void>("DELETE", `/documents/${id}`);

  figures = (id: string) => this.get(`/documents/${id}/figures`, z.array(S.FigureSchema));
  /** Figures need the Bearer header, so they are fetched as a blob (an <img src> cannot send it). */
  async figureBlobUrl(id: string, index: number): Promise<string> {
    const res = await this.fetchImpl(this.url(`/documents/${id}/figures/${index}`), { headers: this.headers(false) });
    if (!res.ok) return this.fail(res);
    return URL.createObjectURL(await res.blob());
  }

  async uploadDocument(collectionId: string, file: File, department?: string): Promise<S.Upload> {
    const form = new FormData();
    form.append("file", file);
    if (department) form.append("department", department);
    const res = await this.fetchImpl(this.url(`/collections/${collectionId}/documents`), { method: "POST", headers: this.headers(false), body: form });
    if (!res.ok) await this.fail(res);
    return S.UploadSchema.parse(await res.json());
  }

  search = (body: { query: string; mode?: string; top_k?: number; collection_ids?: string[] }) => this.post("/search", S.SearchSchema, body);

  // ---- graph
  entities = (q?: string, limit = 60) => this.get("/graph/entities", z.array(S.EntitySchema), { q, limit });
  entity = (id: string) => this.get(`/graph/entities/${id}`, S.EntityDetailSchema);
  graphQuery = (query: string, depth = 2) => this.post("/graph/query", S.GraphQuerySchema, { query, depth });

  // ---- chat / runs / conversations
  chat = (b: ChatRequest) => this.post("/chat", S.ChatResultSchema, { ...b, stream: false });
  runs = () => this.get("/agents/runs", z.array(S.RunListItemSchema));
  run = (id: string) => this.get(`/agents/runs/${id}`, S.RunInspectSchema);
  resume = (id: string, decision: "approve" | "edit" | "reject", answer?: string, note?: string) =>
    this.post(`/agents/runs/${id}/resume`, S.ChatResultSchema, { decision, answer, note });
  conversations = () => this.get("/conversations", z.array(S.ConversationSchema));
  messages = (id: string) => this.get(`/conversations/${id}/messages`, z.array(S.MessageSchema));
  deleteConversation = (id: string) => this.request<void>("DELETE", `/conversations/${id}`);
  feedback = (run_id: string, rating: 1 | -1, comment = "") => this.post("/feedback", S.FeedbackSchema, { run_id, rating, comment });
  myFeedback = (run_id: string) => this.get(`/feedback/${run_id}`, S.FeedbackSchema);

  /** POST + SSE. Calls onEvent for each event; resolves when the stream ends. Abort with the signal to cancel. */
  async stream(path: string, body: unknown, onEvent: (e: StreamEvent) => void, signal?: AbortSignal): Promise<void> {
    const res = await this.fetchImpl(this.url(path), { method: "POST", headers: this.headers(true), body: JSON.stringify(body), signal });
    if (!res.ok) await this.fail(res);
    if (!res.body) throw new ApiError(0, "no_stream", "The server did not return a stream");
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    const parser = new SseParser();
    const handle = (text: string) => {
      for (const ev of parser.push(text)) {
        const payload = JSON.parse(ev.data) as Record<string, unknown>;
        if (ev.event === "done") payload["result"] = S.ChatResultSchema.parse(payload["result"]);
        onEvent({ ...payload, type: ev.event } as unknown as StreamEvent);
      }
    };
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      handle(dec.decode(value, { stream: true }));
    }
    handle(dec.decode());
  }
  streamChat = (b: ChatRequest, onEvent: (e: StreamEvent) => void, signal?: AbortSignal) => this.stream("/chat", { ...b, stream: true }, onEvent, signal);
  streamResume = (id: string, decision: "approve" | "edit" | "reject", onEvent: (e: StreamEvent) => void, answer?: string, note?: string, signal?: AbortSignal) =>
    this.stream(`/agents/runs/${id}/resume?stream=true`, { decision, answer, note }, onEvent, signal);

  // ---- memory
  memory = (kind?: string) => this.get("/memory", z.array(S.MemoryItemSchema), { kind });
  setPreference = (key: string, value: unknown) => this.request<unknown>("PUT", `/memory/preferences/${encodeURIComponent(key)}`, { body: { value } });
  deleteMemoryItem = (id: string) => this.request<void>("DELETE", `/memory/items/${id}`);
  forget = (kind?: string, includeConversations = false) =>
    this.request<{ deleted_items: number; deleted_conversations: number }>("DELETE", "/memory", { query: { kind, include_conversations: includeConversations || undefined } });
  recentDocuments = () => this.get("/memory/documents/recent", z.array(S.RecentDocSchema));
  contexts = () => this.get("/memory/research-contexts", z.array(S.ContextListItemSchema));
  createContext = (name: string, goal = "") => this.post("/memory/research-contexts", z.object({ id: z.string(), name: z.string(), goal: z.string() }), { name, goal });
  context = (id: string) => this.get(`/memory/research-contexts/${id}`, S.ContextDetailSchema);
  deleteContext = (id: string) => this.request<void>("DELETE", `/memory/research-contexts/${id}`);
  addFinding = (contextId: string, chunk_id: string, note = "") => this.request<unknown>("POST", `/memory/research-contexts/${contextId}/findings`, { body: { chunk_id, note } });
  pinDocument = (contextId: string, document_id: string) => this.request<void>("POST", `/memory/research-contexts/${contextId}/pin`, { body: { document_id } });

  // ---- reports
  reports = () => this.get("/reports", z.array(S.ReportListItemSchema));
  report = (id: string) => this.get(`/reports/${id}`, S.ReportSchema);
  createReport = (b: { topic: string; title?: string; max_documents?: number; include_graph?: boolean }) =>
    this.post("/reports", z.object({ id: z.string(), status: z.string(), title: z.string() }), b);
  deleteReport = (id: string) => this.request<void>("DELETE", `/reports/${id}`);
  async exportReport(id: string, format: "md" | "docx" | "pdf"): Promise<{ blob: Blob; filename: string }> {
    const res = await this.fetchImpl(this.url(`/reports/${id}/export`, { format }), { headers: this.headers(false) });
    if (!res.ok) await this.fail(res);
    const m = /filename="([^"]+)"/.exec(res.headers.get("content-disposition") ?? "");
    return { blob: await res.blob(), filename: m?.[1] ?? `report.${format}` };
  }

  // ---- admin / evaluation
  adminOverview = () => this.get("/admin/overview", S.AdminOverviewSchema);
  adminUsers = () => this.get("/admin/users", z.array(S.AdminUserSchema));
  updateUser = (id: string, patch: { role?: string; is_active?: boolean }) => this.request<unknown>("PATCH", `/admin/users/${id}`, { body: patch });
  audit = (action?: string, limit = 50, offset = 0) => this.get("/admin/audit", z.array(S.AuditEntrySchema), { action, limit, offset });
  agenticRun = (goal: string, collection_ids?: string[] | null) => this.post("/agentic/runs", S.AgenticRunSchema, { goal, collection_ids: collection_ids ?? null });
  connectors = () => this.get("/connectors", z.array(S.ConnectorSchema));
  syncConnector = (id: string, reset = false) => this.post(`/connectors/${id}/sync?reset=${reset}`, z.object({ status: z.string() }), {});
  connectorItems = (id: string, status?: string) => this.get(`/connectors/${id}/items`, z.array(S.ConnectorItemSchema), { status });
  adminJobs = (status?: string) => this.get("/admin/jobs", z.array(S.AdminJobSchema), { status });
  evaluation = () => this.get("/evaluation/results", S.EvaluationResultsSchema);
  health = () => this.get("/health/ready", z.object({ status: z.string() }));
}
