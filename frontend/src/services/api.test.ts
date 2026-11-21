import { ApiClient, ApiError } from "./api";
import { chatResult } from "../test/helpers";

const json = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", ...headers } });

function client(fetchImpl: typeof fetch, token: string | null = "tok", onUnauthorized = () => {}) {
  return new ApiClient("/api", () => token, onUnauthorized, fetchImpl);
}

describe("ApiClient", () => {
  it("sends the bearer token and builds query strings without empty values", async () => {
    const f = vi.fn().mockImplementation(() => Promise.resolve(json([])));   // fresh Response per call: bodies are single-use
    await client(f).documents("col1", 50);
    const [url, init] = f.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/documents?collection_id=col1&limit=50");
    expect((init.headers as Record<string, string>)["Authorization"]).toBe("Bearer tok");
    await client(f).entities(undefined, 10);
    expect((f.mock.calls[1] as [string])[0]).toBe("/api/graph/entities?limit=10");
  });

  it("omits Authorization when signed out and sets JSON content-type only for bodies", async () => {
    const f = vi.fn().mockResolvedValue(json({ access_token: "a", token_type: "bearer", expires_in: 1 }));
    await client(f, null).login("a@b.co", "pw");
    const init = (f.mock.calls[0] as [string, RequestInit])[1];
    expect(init.headers).toEqual({ "Content-Type": "application/json" });
    expect(init.body).toBe(JSON.stringify({ email: "a@b.co", password: "pw" }));
  });

  it("maps backend error envelopes and FastAPI validation errors to ApiError", async () => {
    const f1 = vi.fn().mockResolvedValue(json({ error: { code: "not_found", message: "Document not found", details: {} } }, 404));
    await expect(client(f1).document("x")).rejects.toMatchObject({ name: "ApiError", status: 404, code: "not_found", message: "Document not found" });
    const f2 = vi.fn().mockResolvedValue(json({ detail: [{ loc: ["body", "organization_name"], msg: "String should have at least 2 characters" }] }, 422));
    await expect(client(f2).register("A", "a@b.co", "x".repeat(12))).rejects.toMatchObject({ status: 422, message: "organization_name: String should have at least 2 characters" });
    const f3 = vi.fn().mockResolvedValue(new Response("<html>boom</html>", { status: 502 }));
    await expect(client(f3).me()).rejects.toMatchObject({ status: 502, message: "Request failed (502)" });
  });

  it("calls onUnauthorized on 401 so the app can sign the user out", async () => {
    const out = vi.fn();
    const f = vi.fn().mockResolvedValue(json({ error: { code: "authentication_failed", message: "Invalid or expired token" } }, 401));
    await expect(client(f, "bad", out).me()).rejects.toBeInstanceOf(ApiError);
    expect(out).toHaveBeenCalledOnce();
  });

  it("validates responses against the zod schema (API drift fails loudly)", async () => {
    const f = vi.fn().mockResolvedValue(json({ id: "u1" }));
    await expect(client(f).me()).rejects.toThrow();
  });

  it("handles 204 responses", async () => {
    const f = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    await expect(client(f).deleteDocument("d1")).resolves.toBeUndefined();
  });

  it("uploads as multipart without forcing a JSON content-type", async () => {
    const f = vi.fn().mockResolvedValue(json({ outcome: "created", document: { id: "d", collection_id: "c", title: "t", filename: "t.txt", doc_type: "txt", department: null, language: null, status: "processing", current_version: 0, created_at: "x" }, job: null }));
    const file = new File(["hello"], "t.txt", { type: "text/plain" });
    await client(f).uploadDocument("c1", file, "legal");
    const [url, init] = f.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/collections/c1/documents");
    expect(init.body).toBeInstanceOf(FormData);
    expect((init.body as FormData).get("department")).toBe("legal");
    expect(init.headers).not.toHaveProperty("Content-Type");
  });

  it("exports reports and extracts the server-provided filename", async () => {
    const f = vi.fn().mockResolvedValue(new Response("%PDF", { status: 200, headers: { "content-disposition": 'attachment; filename="my_report.pdf"' } }));
    const { blob, filename } = await client(f).exportReport("r1", "pdf");
    expect(filename).toBe("my_report.pdf");
    expect(blob.size).toBe(4);
  });

  it("streams SSE events in order, tolerating arbitrary chunking, and validates the final result", async () => {
    const result = chatResult();
    const text =
      'event: run\ndata: {"run_id":"r1","conversation_id":"cv1"}\n\n' +
      'event: agent\ndata: {"node":"classify","agent":"orchestrator","ms":1,"detail":{}}\n\n' +
      'event: delta\ndata: {"text":"Hello "}\n\n' +
      `event: done\ndata: ${JSON.stringify({ result })}\n\n`;
    const enc = new TextEncoder().encode(text);
    const stream = new ReadableStream<Uint8Array>({ start(c) { for (let i = 0; i < enc.length; i += 7) c.enqueue(enc.slice(i, i + 7)); c.close(); } });
    const f = vi.fn().mockResolvedValue(new Response(stream, { status: 200, headers: { "content-type": "text/event-stream" } }));
    const events: string[] = [];
    await client(f).streamChat({ message: "hi" }, (e) => events.push(e.type));
    expect(events).toEqual(["run", "agent", "delta", "done"]);
    expect(JSON.parse((f.mock.calls[0] as [string, RequestInit])[1].body as string)).toMatchObject({ message: "hi", stream: true });
  });

  it("surfaces HTTP errors from stream endpoints before reading the body", async () => {
    const f = vi.fn().mockResolvedValue(json({ error: { code: "not_found", message: "Conversation not found" } }, 404));
    await expect(client(f).streamChat({ message: "x" }, () => {})).rejects.toMatchObject({ status: 404 });
  });
});
