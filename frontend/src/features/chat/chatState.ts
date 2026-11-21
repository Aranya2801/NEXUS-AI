import type { ChatResult, Citation, Message, StreamEvent } from "../../types/schemas";

export type CitationLite = Partial<Citation> & { id: number };

export interface ChatMsg {
  id: string;
  role: "user" | "assistant";
  text: string;
  citations: CitationLite[];
  result?: ChatResult;
  streaming?: boolean;
  trace: { agent: string | null; node: string }[];
  evidenceCount?: number;
  reviewPending?: boolean;
  error?: string;
  runId?: string;
}

export type ChatAction =
  | { type: "load"; messages: ChatMsg[] }
  | { type: "user"; id: string; text: string }
  | { type: "assistant_start"; id: string }
  | { type: "event"; id: string; event: StreamEvent }
  | { type: "end"; id: string }
  | { type: "fail"; id: string; message: string };

export function messageFromServer(m: Message): ChatMsg {
  return { id: m.id, role: m.role === "user" ? "user" : "assistant", text: m.content, citations: m.citations, trace: [] };
}

function patch(state: ChatMsg[], id: string, f: (m: ChatMsg) => ChatMsg): ChatMsg[] {
  return state.map((m) => (m.id === id ? f(m) : m));
}

export function chatReducer(state: ChatMsg[], a: ChatAction): ChatMsg[] {
  switch (a.type) {
    case "load":
      return a.messages;
    case "user":
      return [...state, { id: a.id, role: "user", text: a.text, citations: [], trace: [] }];
    case "assistant_start":
      return [...state, { id: a.id, role: "assistant", text: "", citations: [], trace: [], streaming: true }];
    case "end":
      return patch(state, a.id, (m) => ({ ...m, streaming: false }));
    case "fail":
      return patch(state, a.id, (m) => ({ ...m, streaming: false, error: a.message }));
    case "event":
      return patch(state, a.id, (m) => {
        const e = a.event;
        switch (e.type) {
          case "run": return { ...m, runId: e.run_id };
          case "agent": return { ...m, trace: [...m.trace, { agent: e.agent, node: e.node }] };
          case "evidence": return { ...m, evidenceCount: e.count };
          case "delta": return { ...m, text: m.text + e.text };
          case "review_required": return { ...m, reviewPending: true };
          case "error": return { ...m, streaming: false, error: e.message };
          case "done": // the final result is authoritative: it replaces streamed text, trace and citations
            return { ...m, streaming: false, result: e.result, runId: e.result.run_id, text: e.result.answer, citations: e.result.citations,
              trace: e.result.trace.map((t) => ({ agent: t.agent, node: t.node })), reviewPending: e.result.status === "needs_review" };
          default: return m;
        }
      });
  }
}
