import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ApiClient } from "../../services/api";
import type { User } from "../../types/schemas";

const TOKEN_KEY = "nexus.token";
// sessionStorage (cleared when the tab closes) limits JWT exposure compared with localStorage. A production deployment should
// prefer an httpOnly cookie issued by the backend; this app uses bearer tokens, so the trade-off is documented in the README.
const store = { get: () => sessionStorage.getItem(TOKEN_KEY), set: (t: string) => sessionStorage.setItem(TOKEN_KEY, t), clear: () => sessionStorage.removeItem(TOKEN_KEY) };

interface AuthValue {
  api: ApiClient;
  user: User | null;
  loading: boolean;
  login(email: string, password: string): Promise<void>;
  register(org: string, email: string, password: string, fullName?: string): Promise<void>;
  logout(): void;
}

const Ctx = createContext<AuthValue | null>(null);

export function AuthProvider({ children, client }: { children: ReactNode; client?: ApiClient }) {
  const tokenRef = useRef<string | null>(store.get());
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState<boolean>(tokenRef.current !== null);

  const logout = useCallback(() => {
    tokenRef.current = null;
    store.clear();
    setUser(null);
  }, []);

  const api = useMemo(
    () => client ?? new ApiClient(import.meta.env.VITE_API_URL ?? "/api", () => tokenRef.current, logout),
    [client, logout],
  );

  useEffect(() => {
    if (!tokenRef.current) return;
    let cancelled = false;
    api.me().then((u) => !cancelled && setUser(u)).catch(() => !cancelled && logout()).finally(() => !cancelled && setLoading(false));
    return () => { cancelled = true; };
  }, [api, logout]);

  const login = useCallback(async (email: string, password: string) => {
    const t = await api.login(email, password);
    tokenRef.current = t.access_token;
    store.set(t.access_token);
    setUser(await api.me());
  }, [api]);

  const register = useCallback(async (org: string, email: string, password: string, fullName = "") => {
    await api.register(org, email, password, fullName);
    await login(email, password);
  }, [api, login]);

  const value = useMemo(() => ({ api, user, loading, login, register, logout }), [api, user, loading, login, register, logout]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth must be used inside <AuthProvider>");
  return v;
}

export const useApi = () => useAuth().api;
