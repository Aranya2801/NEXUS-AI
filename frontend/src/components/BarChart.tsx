export interface Bar { label: string; value: number; low?: number; high?: number }

/** Horizontal bar chart in plain SVG/HTML (no chart library). `low/high` draw an optional confidence interval. */
export function BarChart({ title, bars, max, format = (v: number) => v.toFixed(3) }: { title: string; bars: Bar[]; max?: number; format?: (v: number) => string }) {
  const top = max ?? Math.max(...bars.map((b) => b.high ?? b.value), 1e-9);
  return (
    <figure style={{ margin: 0 }} aria-label={title}>
      <figcaption className="small muted" style={{ marginBottom: 6 }}>{title}</figcaption>
      <div className="col" style={{ gap: 6 }}>
        {bars.map((b) => (
          <div key={b.label} className="row" style={{ gap: 8 }}>
            <span className="small" style={{ width: 150, flex: "none" }}>{b.label}</span>
            <div className="grow" style={{ position: "relative", height: 14, background: "var(--panel-2)", borderRadius: 4 }}>
              <div style={{ width: `${(b.value / top) * 100}%`, height: "100%", background: "var(--accent)", borderRadius: 4 }} role="presentation" data-value={b.value} />
              {b.low !== undefined && b.high !== undefined && (
                <div style={{ position: "absolute", top: 6, height: 2, left: `${(b.low / top) * 100}%`, width: `${((b.high - b.low) / top) * 100}%`, background: "var(--ink)" }} />
              )}
            </div>
            <span className="mono small" style={{ width: 64, textAlign: "right" }}>{format(b.value)}</span>
          </div>
        ))}
      </div>
    </figure>
  );
}
