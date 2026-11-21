export function pageLabel(start: number | null | undefined, end?: number | null): string {
  if (!start) return "";
  return !end || end === start ? `p.${start}` : `pp.${start}–${end}`;
}

export function pct(x: number | null | undefined, digits = 0): string {
  return x === null || x === undefined ? "–" : `${(x * 100).toFixed(digits)}%`;
}

export function ms(x: number | null | undefined): string {
  if (x === null || x === undefined) return "–";
  return x >= 1000 ? `${(x / 1000).toFixed(2)} s` : `${x.toFixed(0)} ms`;
}

export function relTime(iso: string, now: Date = new Date()): string {
  const s = Math.round((now.getTime() - new Date(iso).getTime()) / 1000);
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

export function truncate(s: string, n: number): string {
  return s.length <= n ? s : `${s.slice(0, n - 1)}…`;
}

export type Tone = "good" | "warn" | "bad" | "info" | "neutral";

export function answerTone(status: string | null | undefined): Tone {
  switch (status) {
    case "grounded": return "good";
    case "partial": case "reviewer_edited": return "warn";
    case "insufficient_evidence": case "no_evidence": case "rejected_by_reviewer": return "bad";
    default: return "neutral";
  }
}

export function statusTone(status: string): Tone {
  if (["ready", "completed", "succeeded", "approved"].includes(status)) return "good";
  if (["failed", "dead", "degraded"].includes(status)) return "bad";
  if (["needs_review", "queued", "pending", "processing", "running"].includes(status)) return "warn";
  return "neutral";
}

export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

/** Splits text into plain and [n] citation-marker segments: "x [1][2] y" → text, cite 1, cite 2, text. */
export function splitCitations(text: string): Array<{ kind: "text"; value: string } | { kind: "cite"; id: number }> {
  const out: Array<{ kind: "text"; value: string } | { kind: "cite"; id: number }> = [];
  const re = /\[(\d{1,3})\]/g;
  let last = 0;
  for (let m = re.exec(text); m; m = re.exec(text)) {
    if (m.index > last) out.push({ kind: "text", value: text.slice(last, m.index) });
    out.push({ kind: "cite", id: Number(m[1]) });
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push({ kind: "text", value: text.slice(last) });
  return out;
}
