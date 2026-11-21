import { useCallback, useReducer, useRef, useState } from "react";
import type { ApiClient, ChatRequest } from "../../services/api";
import type { StreamEvent } from "../../types/schemas";
import { chatReducer, type ChatMsg } from "./chatState";

let seq = 0;
const nid = (p: string) => `${p}-${Date.now()}-${++seq}`;

export function useChatStream(api: ApiClient) {
  const [messages, dispatch] = useReducer(chatReducer, [] as ChatMsg[]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const ctl = useRef<AbortController | null>(null);

  const run = useCallback(async (id: string, starter: (onEvent: (e: StreamEvent) => void, signal: AbortSignal) => Promise<void>) => {
    ctl.current = new AbortController();
    setBusy(true);
    try {
      await starter((event) => {
        if (event.type === "run") setConversationId(event.conversation_id);
        dispatch({ type: "event", id, event });
      }, ctl.current.signal);
      dispatch({ type: "end", id });
    } catch (e) {
      if ((e as Error).name === "AbortError") dispatch({ type: "end", id });
      else dispatch({ type: "fail", id, message: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  }, []);

  const send = useCallback(async (message: string, opts: Omit<ChatRequest, "message"> = {}) => {
    dispatch({ type: "user", id: nid("u"), text: message });
    const id = nid("a");
    dispatch({ type: "assistant_start", id });
    await run(id, (onEvent, signal) => api.streamChat({ message, conversation_id: conversationId, ...opts }, onEvent, signal));
    return id;
  }, [api, conversationId, run]);

  /** Resumes a paused run in place: the same assistant message continues streaming into its final answer. */
  const resume = useCallback(async (id: string, runId: string, decision: "approve" | "edit" | "reject", answer?: string, note?: string) => {
    dispatch({ type: "event", id, event: { type: "delta", text: "" } });
    await run(id, (onEvent, signal) => api.streamResume(runId, decision, (e) => {
      // the final `done` result replaces the paused one; deltas are ignored because `done` carries the full approved answer
      if (e.type !== "delta") onEvent(e);
    }, answer, note, signal));
  }, [api, run]);

  const abort = useCallback(() => ctl.current?.abort(), []);
  const reset = useCallback((convId: string | null, loaded: ChatMsg[] = []) => { setConversationId(convId); dispatch({ type: "load", messages: loaded }); }, []);
  return { messages, conversationId, busy, send, resume, abort, reset };
}
