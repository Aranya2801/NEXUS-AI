import { useEffect, useState } from "react";
import { NavLink, Outlet, useMatch } from "react-router-dom";
import { useAuth } from "../features/auth/AuthContext";
import { Badge } from "./ui";

const THEME_KEY = "nexus.theme";

function useTheme() {
  const [theme, setTheme] = useState<string>(() => localStorage.getItem(THEME_KEY) ?? (window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
  useEffect(() => { document.documentElement.dataset["theme"] = theme; localStorage.setItem(THEME_KEY, theme); }, [theme]);
  return [theme, () => setTheme((t) => (t === "dark" ? "light" : "dark"))] as const;
}

const NAV: { label: string; items: { to: string; text: string; admin?: boolean }[] }[] = [
  { label: "Research", items: [{ to: "/", text: "Dashboard" }, { to: "/agent", text: "Autonomous agent" }, { to: "/chat", text: "Research chat" }, { to: "/reports", text: "Reports" }] },
  { label: "Knowledge", items: [{ to: "/documents", text: "Documents" }, { to: "/explorer", text: "Knowledge explorer" }, { to: "/citations", text: "Citation explorer" }] },
  { label: "Operations", items: [{ to: "/runs", text: "Agent runs" }, { to: "/evaluation", text: "Evaluation" }] },
  { label: "Account", items: [{ to: "/settings", text: "Settings & memory" }, { to: "/connectors", text: "Data connectors", admin: true }, { to: "/admin", text: "Administration", admin: true }] },
];

export function Layout() {
  const { user, logout } = useAuth();
  const [theme, toggle] = useTheme();
  const inChat = useMatch("/chat/*") !== null;
  return (
    <div className="shell">
      <aside className="sidebar" aria-label="Primary">
        <div className="brand"><span className="brand-mark" aria-hidden />NEXUS AI</div>
        <nav className="nav">
          {NAV.map((g) => (
            <div key={g.label}>
              <div className="nav-label">{g.label}</div>
              {g.items.filter((i) => !i.admin || user?.role === "admin").map((i) => (
                <NavLink key={i.to} to={i.to} end={i.to === "/"} className={({ isActive }) => (isActive ? "active" : "")}>{i.text}</NavLink>
              ))}
            </div>
          ))}
        </nav>
      </aside>
      <div className="main">
        <header className="topbar">
          <span className="muted small">Evidence-grounded research workbench</span>
          <div className="row right">
            <Badge tone="info">{user?.role}</Badge>
            <span className="small">{user?.email}</span>
            <button className="btn small" onClick={toggle} aria-label="Toggle colour theme">{theme === "dark" ? "Light" : "Dark"}</button>
            <button className="btn small" onClick={logout}>Sign out</button>
          </div>
        </header>
        <main className={`page${inChat ? " flush" : ""}`} id="main"><Outlet /></main>
      </div>
    </div>
  );
}
