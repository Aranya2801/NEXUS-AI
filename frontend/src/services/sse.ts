export interface SseEvent {
  event: string;
  data: string;
}

/**
 * Incremental Server-Sent-Events parser. Network chunks may split an event anywhere (even mid-line or between \r and \n),
 * so input is buffered until a blank line terminates the event. Supports multi-line `data:` and ignores comments.
 */
export class SseParser {
  private buffer = "";

  push(chunk: string): SseEvent[] {
    this.buffer += chunk;
    const out: SseEvent[] = [];
    for (;;) {
      const m = /\r\n\r\n|\n\n|\r\r/.exec(this.buffer);
      if (!m) break;
      const raw = this.buffer.slice(0, m.index);
      this.buffer = this.buffer.slice(m.index + m[0].length);
      const ev = parseBlock(raw);
      if (ev) out.push(ev);
    }
    return out;
  }
}

function parseBlock(raw: string): SseEvent | null {
  let event = "message";
  const data: string[] = [];
  for (const line of raw.split(/\r\n|\n|\r/)) {
    if (line === "" || line.startsWith(":")) continue;
    const i = line.indexOf(":");
    const field = i === -1 ? line : line.slice(0, i);
    const value = i === -1 ? "" : line.slice(i + 1).replace(/^ /, "");
    if (field === "event") event = value;
    else if (field === "data") data.push(value);
  }
  return data.length ? { event, data: data.join("\n") } : null;
}
