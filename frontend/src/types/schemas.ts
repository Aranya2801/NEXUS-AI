/**
 * Runtime schemas for every API response the UI depends on. Types are inferred from them (single source of truth) and the
 * live-backend contract tests (`npm run test:contract`) validate real responses against them, so API drift fails loudly.
 * Unknown extra fields are tolerated (zod strips them); fields the UI relies on are required.
 */
import { z } from "zod";

const nullableNum = z.number().nullable();
const nullableStr = z.string().nullable();

export const TokenSchema = z.object({ access_token: z.string(), token_type: z.string(), expires_in: z.number() });
export const UserSchema = z.object({
  id: z.string(), org_id: z.string(), email: z.string(), full_name: z.string(), role: z.enum(["admin", "editor", "viewer"]), department: nullableStr,
});
export const CollectionSchema = z.object({ id: z.string(), name: z.string(), description: z.string(), is_restricted: z.boolean() });
export const JobSchema = z.object({
  id: z.string(), document_id: z.string(), status: z.string(), stage: z.string(), attempts: z.number(), max_attempts: z.number(), error: nullableStr,
});
export const DocumentSchema = z.object({
  id: z.string(), collection_id: z.string(), title: z.string(), filename: z.string(), doc_type: z.string(), department: nullableStr,
  language: nullableStr, status: z.string(), current_version: z.number(), created_at: z.string(),
});
export const UploadSchema = z.object({ outcome: z.enum(["created", "new_version", "duplicate"]), document: DocumentSchema, job: JobSchema.nullable().optional() });
export const ChunkSchema = z.object({
  id: z.string(), level: z.string(), kind: z.string().default("text"), ordinal: z.number(), text: z.string(), page_start: nullableNum, page_end: nullableNum, section: nullableStr,
  char_start: z.number(), char_end: z.number(), token_count: z.number(), version: z.number(), is_current: z.boolean(),
});

export const CitationSchema = z.object({
  id: z.number(), chunk_id: z.string(), document_id: z.string(), title: z.string(), filename: z.string(), page_start: nullableNum, page_end: nullableNum,
  section: nullableStr, version: z.number(), is_current: z.boolean(), quote: z.string(), quotes: z.array(z.string()).default([]),
});
export const ClaimVerdictSchema = z.object({
  index: z.number(), text: z.string(), status: z.enum(["supported", "weak", "unsupported"]), reason: z.string(), coverage: z.number(), judge: z.boolean().nullable().optional(),
});
export const VerificationSchema = z.object({
  supported: z.number().optional(), weak: z.number().optional(), unsupported: z.number().optional(), claims_total: z.number().optional(),
  claims_kept: z.number().optional(), citation_coverage: z.number().optional(), judge_used: z.boolean().optional(),
  claims: z.array(ClaimVerdictSchema).optional(), unsupported_removed: z.array(ClaimVerdictSchema.partial()).optional(),
});
export const TraceEntrySchema = z.object({ agent: z.string(), node: z.string(), ms: z.number() }).passthrough();
export const EvidenceItemSchema = z.object({
  chunk_id: z.string(), document_id: z.string(), title: z.string(), page_start: nullableNum, section: nullableStr, version: z.number(),
  score: z.number(), scores: z.record(z.string(), z.number()), ranks: z.record(z.string(), z.number()), snippet: z.string(),
});
export const QualitySchema = z.object({
  relevance: z.number().optional(), completeness: z.number().optional(), groundedness: z.number().optional(), citation_coverage: z.number().optional(),
  evidence_confidence: z.number().optional(), contradictions: z.number().optional(), loop: z.number().optional(), reasons: z.array(z.string()).optional(),
});
export const ChatResultSchema = z.object({
  run_id: z.string(),
  status: z.string(),
  conversation_id: z.string().optional(),
  answer: z.string(),
  answer_status: z.string().nullable().optional(),
  citations: z.array(CitationSchema),
  verification: VerificationSchema,
  quality: QualitySchema,
  analysis: z.record(z.string(), z.unknown()),
  query_class: z.string().nullable().optional(),
  intent: z.string().nullable().optional(),
  plan: z.record(z.string(), z.unknown()).nullable().optional(),
  queries: z.array(z.string()),
  confidence: z.object({ score: z.number(), coverage: z.number(), top_score: z.number(), margin: z.number() }).partial(),
  evidence: z.array(EvidenceItemSchema),
  graph: z.object({ seeds: z.array(z.object({ id: z.string(), name: z.string(), type: z.string() })), edges: z.number(), documents: z.number() }).nullable().optional(),
  injection_sanitized: z.number().optional(),
  review: z.record(z.string(), z.unknown()).nullable().optional(),
  errors: z.array(z.string()),
  trace: z.array(TraceEntrySchema),
  llm_usage: z.object({ calls: z.number().optional(), prompt_tokens: z.number().optional(), completion_tokens: z.number().optional(), by_task: z.record(z.string(), z.number()).optional() }),
});
export const RunInspectSchema = ChatResultSchema.extend({
  query: z.string(), created_at: z.string(), error: nullableStr, next: z.array(z.string()), state_keys: z.array(z.string()), pending_review: z.array(z.record(z.string(), z.unknown())).nullable(),
});
export const RunListItemSchema = z.object({ run_id: z.string(), status: z.string(), query: z.string(), conversation_id: nullableStr, created_at: z.string() });
export const ConversationSchema = z.object({ id: z.string(), title: z.string(), updated_at: z.string() });
export const MessageSchema = z.object({
  id: z.string(), role: z.string(), content: z.string(), citations: z.array(CitationSchema.partial().extend({ id: z.number() })), verification: z.record(z.string(), z.unknown()), created_at: z.string(),
});

export const FigureSchema = z.object({
  index: z.number(), page: nullableNum, source: z.string(), width: z.number(), height: z.number(), url: z.string(),
  ocr: z.object({ status: z.string(), chars: z.number(), confidence: z.number().nullable().optional() }).nullable().optional(),
  caption: z.object({ status: z.string(), model: z.string().optional(), chars: z.number().optional(), error: z.string().optional() }).nullable().optional(),
});

export const HitSchema = z.object({
  chunk_id: z.string(), document_id: z.string(), collection_id: z.string(), title: z.string(), filename: z.string(), version: z.number(), is_current: z.boolean(),
  page_start: nullableNum, page_end: nullableNum, section: nullableStr, text: z.string(), parent_text: nullableStr, kind: z.string().default("text"), score: z.number(),
  scores: z.record(z.string(), z.number()), ranks: z.record(z.string(), z.number()),
});
export const SearchSchema = z.object({ mode: z.string(), hits: z.array(HitSchema), timings_ms: z.record(z.string(), z.number()), candidates: z.record(z.string(), z.number()), collections_searched: z.number() });

export const EntitySchema = z.object({ id: z.string(), name: z.string(), type: z.string(), aliases: z.array(z.string()), mentions: z.number() });
export const EntityDetailSchema = z.object({
  id: z.string(), name: z.string(), type: z.string(), aliases: z.array(z.string()),
  mentions: z.array(z.object({ chunk_id: z.string(), document_id: z.string(), page: nullableNum, section: nullableStr, title: z.string() })),
  edges: z.array(z.object({ source: nullableStr, predicate: z.string(), target: nullableStr, confidence: z.number(), document_id: z.string(), title: z.string(), page: nullableNum, evidence: nullableStr })),
});
export const GraphQuerySchema = z.object({
  seeds: z.array(z.object({ id: z.string(), name: z.string(), type: z.string() })),
  nodes: z.record(z.string(), z.object({ id: z.string(), name: z.string().optional(), type: z.string().optional(), depth: z.number() })),
  edges: z.array(z.object({
    source_id: z.string(), predicate: z.string(), target_id: z.string(), confidence: z.number(), depth: z.number(), chunk_id: z.string(), document_id: z.string(),
    title: z.string(), page: nullableNum, evidence: nullableStr,
  })),
  documents: z.number(),
  hits: z.array(z.object({ chunk_id: z.string(), document_id: z.string(), title: z.string(), page_start: nullableNum, section: nullableStr, text: z.string(), score: z.number() })),
});

export const MemoryItemSchema = z.object({ id: z.string(), kind: z.string(), scope_id: z.string(), key: z.string(), value: z.unknown(), expires_at: nullableStr, updated_at: z.string() });
export const RecentDocSchema = z.object({ document_id: z.string(), title: z.string(), filename: z.string(), action: z.string(), count: z.number(), last_at: z.string() });
export const ContextListItemSchema = z.object({ id: z.string(), name: z.string(), goal: z.string(), pinned_documents: z.number() });
export const ContextDetailSchema = z.object({
  id: z.string(), name: z.string(), goal: z.string(), pinned_documents: z.array(z.string()),
  findings: z.array(z.object({
    id: z.string(), hidden: z.boolean(), reason: z.string().optional(), title: z.string().optional(), quote: z.string().optional(), note: z.string().optional(),
    page_start: nullableNum.optional(), document_id: z.string().optional(), chunk_id: z.string().optional(),
  })),
});

const ReportItem = z.object({ text: z.string(), citations: z.array(z.number()) });
export const ReportBlockSchema = z.discriminatedUnion("type", [
  z.object({ type: z.literal("claim"), text: z.string(), citations: z.array(z.number()), status: z.string().optional() }),
  z.object({ type: z.literal("list"), items: z.array(ReportItem) }),
  z.object({ type: z.literal("table"), columns: z.array(z.string()), rows: z.array(z.object({ cells: z.array(z.string()), citations: z.array(z.number()) })) }),
  z.object({ type: z.literal("note"), text: z.string() }),
]);
export const ReportContentSchema = z.object({
  title: z.string(), topic: z.string(), generated_at: z.string(), redacted: z.boolean().optional(),
  documents: z.array(z.object({ document_id: z.string(), title: z.string(), filename: z.string(), versions: z.array(z.number()), pages: z.array(z.number()) })),
  executive_summary: z.array(ReportItem.extend({ section: z.string().optional() })),
  sections: z.array(z.object({ id: z.string(), title: z.string(), blocks: z.array(ReportBlockSchema) })),
  citations: z.array(CitationSchema.partial().extend({ id: z.number(), hidden: z.boolean().optional() })),
  verification: z.object({ claims_total: z.number(), claims_kept: z.number(), unsupported_removed: z.array(z.unknown()), dropped_at_final_check: z.array(z.unknown()) }),
  coverage_notes: z.array(z.string()),
  method: z.object({ statement: z.string(), llm: z.string(), reranker: z.string(), warning: z.string().nullable() }),
});
export const ReportListItemSchema = z.object({ id: z.string(), title: z.string(), topic: z.string(), status: z.string(), created_at: z.string() });
export const ReportSchema = z.object({
  id: z.string(), title: z.string(), topic: z.string(), status: z.string(), progress: z.array(z.object({ step: z.string(), detail: z.string(), at: z.string() })),
  error: nullableStr, content: ReportContentSchema.nullable(), created_at: z.string(), updated_at: z.string(),
});

const counts = z.record(z.string(), z.number());
export const AdminOverviewSchema = z.object({
  counts: z.object({ users: z.number(), collections: z.number(), chunks: z.number(), conversations: z.number() }),
  documents: counts, ingestion_jobs: counts, agent_runs: counts, reports: counts,
  graph: z.object({ entities: z.number(), relations: z.number(), mentions: z.number() }),
  feedback: z.object({ up: z.number(), down: z.number() }),
  runtime: z.object({ env: z.string(), llm: z.string(), embedder: z.string(), reranker: z.string(), graph_extractor: z.string(), human_review_mode: z.string(), max_retrieval_loops: z.number() }),
});
export const AdminUserSchema = z.object({ id: z.string(), email: z.string(), full_name: z.string(), role: z.enum(["admin", "editor", "viewer"]), department: nullableStr, is_active: z.boolean() });
export const AuditEntrySchema = z.object({ id: z.string(), action: z.string(), user_id: nullableStr, resource: nullableStr, detail: z.record(z.string(), z.unknown()), created_at: z.string() });
export const ConnectorSchema = z.object({
  id: z.string(), name: z.string(), kind: z.string(), collection_id: z.string(), acl_mode: z.string(), enabled: z.boolean(),
  interval_minutes: z.number().nullable(), last_sync_at: z.string().nullable(), has_cursor: z.boolean(),
  last_run: z.object({ id: z.string(), status: z.string(), stats: z.record(z.string(), z.number()), error: z.string().nullable(), started_at: z.string(), finished_at: z.string().nullable() }).nullable().optional(),
});
export const ConnectorItemSchema = z.object({ external_id: z.string(), name: z.string(), status: z.string(), detail: z.string().nullable(), document_id: z.string().nullable() });

export const AgenticStepSchema = z.object({
  id: z.string(), wave: z.number(), tool: z.string(), purpose: z.string(), args: z.record(z.string(), z.unknown()), depends_on: z.array(z.string()),
  ok: z.boolean(), error: z.string().nullable(), ms: z.number(), summary: z.string(),
});
export const AgenticRunSchema = z.object({
  id: z.string(), goal: z.string(), status: z.string(), error: z.string().nullable(),
  result: z.object({
    answer: z.string(), answer_status: z.string(), citations: z.array(z.object({ id: z.number(), title: z.string(), filename: z.string(), page_start: nullableNum, quote: z.string() }).passthrough()),
    steps: z.array(AgenticStepSchema), planner: z.array(z.string()), stopped_reason: z.string(),
    budget: z.object({ steps_used: z.number(), max_steps: z.number(), waves_used: z.number(), max_waves: z.number() }),
    computations: z.array(z.record(z.string(), z.unknown())),
  }).nullable(),
});

export const AdminJobSchema = JobSchema.extend({ title: z.string(), updated_at: z.string() });
export const FeedbackSchema = z.object({ run_id: z.string(), rating: z.number().nullable(), comment: z.string() });

/** Shape the Phase-9 evaluation backend must produce at GET /evaluation/results (the UI already renders it). */
const Metrics = z.record(z.string(), z.number());
export const EvaluationResultsSchema = z.object({
  generated_at: z.string(),
  dataset: z.object({ name: z.string(), n_questions: z.number(), synthetic: z.boolean(), description: z.string().optional() }),
  environment: z.object({ llm: z.string(), embedder: z.string(), mock: z.boolean() }),
  systems: z.array(z.object({ id: z.string(), name: z.string(), retrieval: Metrics, generation: Metrics.optional(), engineering: Metrics })),
  notes: z.array(z.string()).default([]),
});

export type Token = z.infer<typeof TokenSchema>;
export type User = z.infer<typeof UserSchema>;
export type Collection = z.infer<typeof CollectionSchema>;
export type Job = z.infer<typeof JobSchema>;
export type DocumentItem = z.infer<typeof DocumentSchema>;
export type Upload = z.infer<typeof UploadSchema>;
export type Chunk = z.infer<typeof ChunkSchema>;
export type Figure = z.infer<typeof FigureSchema>;
export type Citation = z.infer<typeof CitationSchema>;
export type ClaimVerdict = z.infer<typeof ClaimVerdictSchema>;
export type TraceEntry = z.infer<typeof TraceEntrySchema>;
export type ChatResult = z.infer<typeof ChatResultSchema>;
export type RunInspect = z.infer<typeof RunInspectSchema>;
export type RunListItem = z.infer<typeof RunListItemSchema>;
export type Conversation = z.infer<typeof ConversationSchema>;
export type Message = z.infer<typeof MessageSchema>;
export type Hit = z.infer<typeof HitSchema>;
export type Search = z.infer<typeof SearchSchema>;
export type Entity = z.infer<typeof EntitySchema>;
export type EntityDetail = z.infer<typeof EntityDetailSchema>;
export type GraphQuery = z.infer<typeof GraphQuerySchema>;
export type MemoryItem = z.infer<typeof MemoryItemSchema>;
export type RecentDoc = z.infer<typeof RecentDocSchema>;
export type ContextListItem = z.infer<typeof ContextListItemSchema>;
export type ContextDetail = z.infer<typeof ContextDetailSchema>;
export type ReportBlock = z.infer<typeof ReportBlockSchema>;
export type ReportContent = z.infer<typeof ReportContentSchema>;
export type Report = z.infer<typeof ReportSchema>;
export type ReportListItem = z.infer<typeof ReportListItemSchema>;
export type AdminOverview = z.infer<typeof AdminOverviewSchema>;
export type AdminUser = z.infer<typeof AdminUserSchema>;
export type AuditEntry = z.infer<typeof AuditEntrySchema>;
export type AdminJob = z.infer<typeof AdminJobSchema>;
export type AgenticRun = z.infer<typeof AgenticRunSchema>;
export type Connector = z.infer<typeof ConnectorSchema>;
export type ConnectorItem = z.infer<typeof ConnectorItemSchema>;
export type EvaluationResults = z.infer<typeof EvaluationResultsSchema>;

export type StreamEvent =
  | { type: "run"; run_id: string; conversation_id: string }
  | { type: "agent"; node: string; agent: string | null; ms: number | null; detail: Record<string, unknown> }
  | { type: "evidence"; count: number; documents: number }
  | { type: "delta"; text: string }
  | { type: "review_required" }
  | { type: "done"; result: ChatResult }
  | { type: "error"; message: string; run_id?: string };
