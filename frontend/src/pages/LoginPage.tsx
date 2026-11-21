import { useState, type FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../features/auth/AuthContext";
import { ErrorBanner, Spinner, Tabs } from "../components/ui";

export const MIN_PASSWORD = 10;

export function LoginPage() {
  const { user, login, register } = useAuth();
  const nav = useNavigate();
  const from = (useLocation().state as { from?: string } | null)?.from ?? "/";
  const [mode, setMode] = useState<"login" | "register">("login");
  const [org, setOrg] = useState("");
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);

  if (user) return <Navigate to={from} replace />;

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(undefined);
    if (mode === "register" && password.length < MIN_PASSWORD) {
      setError(new Error(`Password must be at least ${MIN_PASSWORD} characters`));
      return;
    }
    setBusy(true);
    try {
      if (mode === "login") await login(email.trim(), password);
      else await register(org.trim(), email.trim(), password, name.trim());
      nav(from, { replace: true });
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth">
      <div className="brand" style={{ justifyContent: "center", paddingBottom: 18 }}><span className="brand-mark" aria-hidden />NEXUS AI</div>
      <form className="card" onSubmit={submit} aria-label={mode === "login" ? "Sign in" : "Create organization"}>
        <Tabs tabs={[{ id: "login", label: "Sign in" }, { id: "register", label: "Create organization" }]} active={mode} onChange={setMode} />
        <ErrorBanner error={error} />
        {mode === "register" && (
          <>
            <div className="field"><label htmlFor="org">Organization name</label><input id="org" value={org} onChange={(e) => setOrg(e.target.value)} required minLength={2} /></div>
            <div className="field"><label htmlFor="name">Your name</label><input id="name" value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" /></div>
          </>
        )}
        <div className="field"><label htmlFor="email">Email</label><input id="email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoComplete="email" /></div>
        <div className="field">
          <label htmlFor="password">Password</label>
          <input id="password" type="password" value={password} onChange={(e) => setPassword(e.target.value)} required autoComplete={mode === "login" ? "current-password" : "new-password"} />
          {mode === "register" && <span className="small muted">At least {MIN_PASSWORD} characters. You become the admin of the new organization.</span>}
        </div>
        <button className="btn primary" type="submit" disabled={busy} style={{ width: "100%" }}>{busy ? <Spinner /> : mode === "login" ? "Sign in" : "Create organization"}</button>
        {mode === "register" && <p className="small muted">Joining an existing organization is by invitation: ask an admin to create your account.</p>}
      </form>
    </div>
  );
}
