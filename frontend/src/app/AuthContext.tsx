import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { getSession, logoutSession } from "../services/api";
import type { SessionResponse, SessionUser } from "../types/api";

export type AuthState =
  | { status: "loading" }
  | { status: "anonymous" }
  | { status: "authenticated"; user: SessionUser }
  | { status: "error"; error: Error };

interface AuthContextValue {
  state: AuthState;
  refreshSession: () => Promise<AuthState>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);
let initialSessionRequest: Promise<SessionResponse> | null = null;

function getInitialSession(): Promise<SessionResponse> {
  if (!initialSessionRequest) {
    const request = getSession();
    initialSessionRequest = request;
    void request.then(
      () => {
        if (initialSessionRequest === request) initialSessionRequest = null;
      },
      () => {
        if (initialSessionRequest === request) initialSessionRequest = null;
      },
    );
  }
  return initialSessionRequest;
}

function toError(reason: unknown): Error {
  return reason instanceof Error
    ? reason
    : new Error("Não foi possível consultar a sessão.");
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: "loading" });
  const requestVersion = useRef(0);
  const mounted = useRef(false);

  const loadSession = useCallback(async (
    initialLoad: boolean,
  ): Promise<AuthState> => {
    const version = ++requestVersion.current;
    setState({ status: "loading" });

    try {
      const session = await (
        initialLoad ? getInitialSession() : getSession()
      );
      const nextState: AuthState = session.authenticated
        ? { status: "authenticated", user: session.user }
        : { status: "anonymous" };
      if (mounted.current && version === requestVersion.current) {
        setState(nextState);
      }
      return nextState;
    } catch (reason) {
      const nextState: AuthState = {
        status: "error",
        error: toError(reason),
      };
      if (mounted.current && version === requestVersion.current) {
        setState(nextState);
      }
      return nextState;
    }
  }, []);

  const refreshSession = useCallback(
    () => loadSession(false),
    [loadSession],
  );

  const logout = useCallback(async (): Promise<void> => {
    await logoutSession();
    requestVersion.current += 1;
    if (mounted.current) setState({ status: "anonymous" });
  }, []);

  useEffect(() => {
    mounted.current = true;
    void loadSession(true);

    return () => {
      mounted.current = false;
      requestVersion.current += 1;
    };
  }, [loadSession]);

  return (
    <AuthContext.Provider value={{ state, refreshSession, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth precisa ser usado dentro de AuthProvider.");
  }
  return context;
}
