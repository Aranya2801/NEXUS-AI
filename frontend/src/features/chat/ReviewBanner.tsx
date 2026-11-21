import { useState } from "react";

interface Props {
  answer: string;
  reasons: string[];
  busy?: boolean;
  onDecide: (decision: "approve" | "edit" | "reject", answer?: string, note?: string) => void;
}

/** Human-in-the-loop controls shown when the workflow is paused at `human_review`. */
export function ReviewBanner({ answer, reasons, busy, onDecide }: Props) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(answer);
  const [note, setNote] = useState("");
  return (
    <div className="banner warn" role="region" aria-label="Human review required">
      <b>Waiting for human review.</b> {reasons.length > 0 && <span>Why: {reasons.join(", ")}. </span>}Nothing is saved to memory until the answer is approved.
      {editing && (
        <div className="field" style={{ marginTop: 8 }}>
          <label htmlFor="edit-answer">Edited answer</label>
          <textarea id="edit-answer" value={text} onChange={(e) => setText(e.target.value)} rows={5} />
        </div>
      )}
      <div className="field" style={{ marginTop: 8 }}><label htmlFor="review-note">Note (optional)</label><input id="review-note" value={note} onChange={(e) => setNote(e.target.value)} /></div>
      <div className="row">
        <button className="btn primary" disabled={busy} onClick={() => onDecide("approve", undefined, note || undefined)}>Approve</button>
        {editing ? <button className="btn" disabled={busy || !text.trim()} onClick={() => onDecide("edit", text, note || undefined)}>Save edited answer</button> : <button className="btn" disabled={busy} onClick={() => setEditing(true)}>Edit…</button>}
        <button className="btn danger" disabled={busy} onClick={() => onDecide("reject", undefined, note || undefined)}>Reject</button>
      </div>
    </div>
  );
}
