import { useEffect, useRef, type ReactNode } from "react";
import { ApiError } from "../services/api";
import type { Tone } from "../services/format";

export function Badge({ tone = "neutral", children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return <span className={`badge ${tone}`} title={title}>{children}</span>;
}

export function Meter({ value, label, invert = false }: { value: number | null | undefined; label?: string; invert?: boolean }) {
  const v = Math.max(0, Math.min(1, value ?? 0));
  const good = invert ? 1 - v : v;
  const tone = good >= 0.66 ? "good" : good >= 0.33 ? "warn" : "bad";
  return (
    <div role="meter" aria-valuemin={0} aria-valuemax={1} aria-valuenow={v} aria-label={label}>
      <div className={`meter ${tone}`}><i style={{ width: `${v * 100}%` }} /></div>
    </div>
  );
}

export function Spinner({ label = "Loading" }: { label?: string }) {
  return <span className="spinner" role="status" aria-label={label} />;
}

export function EmptyState({ title, hint, action }: { title: string; hint?: ReactNode; action?: ReactNode }) {
  return (
    <div className="empty">
      <h3>{title}</h3>
      {hint && <div className="small">{hint}</div>}
      {action && <div style={{ marginTop: 12 }}>{action}</div>}
    </div>
  );
}

export function errorMessage(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  return e instanceof Error ? e.message : String(e);
}

export function ErrorBanner({ error }: { error: unknown }) {
  if (!error) return null;
  return <div className="banner" role="alert">{errorMessage(error)}</div>;
}

export function Loading({ children, loading, error, empty }: { children: ReactNode; loading: boolean; error?: unknown; empty?: ReactNode }) {
  if (error) return <ErrorBanner error={error} />;
  if (loading) return <div className="row muted"><Spinner /> Loading…</div>;
  return <>{empty ?? children}</>;
}

export function Tabs<T extends string>({ tabs, active, onChange }: { tabs: { id: T; label: ReactNode }[]; active: T; onChange: (t: T) => void }) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((t) => (
        <button key={t.id} role="tab" className="tab" aria-selected={t.id === active} onClick={() => onChange(t.id)}>{t.label}</button>
      ))}
    </div>
  );
}

export function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    ref.current?.querySelector<HTMLElement>("input,select,textarea,button")?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("keydown", onKey); prev?.focus(); };
  }, [onClose]);
  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={title} ref={ref}>
        <h2>{title}</h2>
        {children}
      </div>
    </div>
  );
}

export function PageHead({ title, children }: { title: ReactNode; children?: ReactNode }) {
  return <div className="page-head"><h1>{title}</h1><div className="row right">{children}</div></div>;
}

export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: ReactNode }) {
  return <div className="stat"><span className="muted small">{label}</span><b>{value}</b>{hint && <span className="muted small">{hint}</span>}</div>;
}

export function Json({ value }: { value: unknown }) {
  return <pre className="json">{JSON.stringify(value, null, 2)}</pre>;
}
