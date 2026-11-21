import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { Spinner } from "../../components/ui";
import { useAuth } from "./AuthContext";

export function RequireAuth({ children, role }: { children: ReactNode; role?: "admin" }) {
  const { user, loading } = useAuth();
  const loc = useLocation();
  if (loading) return <div className="empty"><Spinner label="Checking session" /></div>;
  if (!user) return <Navigate to="/login" replace state={{ from: loc.pathname + loc.search }} />;
  if (role === "admin" && user.role !== "admin") {
    return <div className="empty"><h3>Administrators only</h3><div className="small">Your role ({user.role}) cannot open this page.</div></div>;
  }
  return <>{children}</>;
}
