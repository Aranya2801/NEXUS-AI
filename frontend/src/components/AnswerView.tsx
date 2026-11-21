import { splitCitations } from "../services/format";

interface Props {
  text: string;
  citationIds: number[];
  activeId?: number | null;
  onSelect?: (id: number) => void;
  streaming?: boolean;
}

/** Renders an answer with [n] markers as clickable citation chips (only for citations that exist; others stay plain text). */
export function AnswerView({ text, citationIds, activeId, onSelect, streaming }: Props) {
  const known = new Set(citationIds);
  const parts = splitCitations(text);
  return (
    <div className="answer" aria-live={streaming ? "polite" : "off"}>
      <p>
        {parts.map((p, i) => {
          if (p.kind === "cite" && known.has(p.id)) {
            return (
              <button key={i} className={`chip${activeId === p.id ? " active" : ""}`} onClick={() => onSelect?.(p.id)} aria-label={`Citation ${p.id}`}>
                {p.id}
              </button>
            );
          }
          const raw = p.kind === "cite" ? `[${p.id}]` : p.value;
          const bits = raw.split("(limited support)");
          return bits.flatMap((b, j) => (j === 0 ? [b] : [<em key={`${i}-${j}`} className="muted small"> (limited support) </em>, b]));
        })}
        {streaming && <span className="spinner" aria-hidden style={{ marginLeft: 6 }} />}
      </p>
    </div>
  );
}
