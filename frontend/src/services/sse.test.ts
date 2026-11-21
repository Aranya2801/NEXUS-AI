import { SseParser } from "./sse";

const feed = (chunks: string[]) => { const p = new SseParser(); return chunks.flatMap((c) => p.push(c)); };

describe("SseParser", () => {
  it("parses a complete event", () => {
    expect(feed(['event: delta\ndata: {"text":"hi"}\n\n'])).toEqual([{ event: "delta", data: '{"text":"hi"}' }]);
  });
  it("reassembles an event split at every possible byte boundary", () => {
    const raw = 'event: agent\ndata: {"node":"classify"}\n\nevent: done\ndata: {"ok":true}\n\n';
    for (let i = 1; i < raw.length; i++) {
      expect(feed([raw.slice(0, i), raw.slice(i)]).map((e) => e.event)).toEqual(["agent", "done"]);
    }
  });
  it("handles CRLF line endings, including a split between \\r and \\n", () => {
    expect(feed(["event: a\r\ndata: 1\r", "\n\r\n"])).toEqual([{ event: "a", data: "1" }]);
  });
  it("joins multi-line data and defaults the event name to 'message'", () => {
    expect(feed(["data: line1\ndata: line2\n\n"])).toEqual([{ event: "message", data: "line1\nline2" }]);
  });
  it("ignores comments, unknown fields and blocks without data; keeps a trailing partial event buffered", () => {
    const p = new SseParser();
    expect(p.push(": keepalive\n\nid: 5\nevent: x\n\nevent: y\ndata: 2\n\nevent: z\ndata: part")).toEqual([{ event: "y", data: "2" }]);
    expect(p.push("ial\n\n")).toEqual([{ event: "z", data: "partial" }]);
  });
  it("keeps a colon inside the data value", () => {
    expect(feed(['data: {"a":"b:c"}\n\n'])[0]?.data).toBe('{"a":"b:c"}');
  });
});
