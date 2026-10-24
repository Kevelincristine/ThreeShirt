import type { ReactNode } from "react";
import { Link, Navigate } from "react-router-dom";
import { Alert } from "../components/feedback/Alert";
import { Loading } from "../components/feedback/Loading";
import { useAuth } from "./AuthContext";

export function RequireStudent({ children }: { children: ReactNode }) {
  const { state, refreshSession } = useAuth();

  if (state.status === "loading") {
    return (
      <section aria-labelledby="student-access-title" className="paper-panel">
        <h1 id="student-access-title">Área do estudante</h1>
        <Loading label="Verificando sua sessão..." />
      </section>
    );
  }

  if (state.status === "error") {
    return (
      <section aria-labelledby="student-access-title" className="paper-panel">
        <h1 id="student-access-title">Área do estudante</h1>
        <Alert variant="error">
          Não foi possível verificar sua sessão. A área permanece protegida.
        </Alert>
        <button
          className="button button--primary access-retry"
          onClick={() => void refreshSession()}
          type="button"
        >
          Tentar novamente
        </button>
      </section>
    );
  }

  if (state.status === "anonymous") {
    return <Navigate replace to="/login" />;
  }

  if (state.user.role !== "student") {
    return (
      <section aria-labelledby="student-access-title" className="paper-panel">
        <p className="eyebrow">Acesso restrito</p>
        <h1 id="student-access-title">Área exclusiva para estudantes</h1>
        <Alert variant="warning">
          Sua conta está autenticada, mas não possui o perfil de estudante
          necessário para acessar esta área.
        </Alert>
        <Link className="button button--secondary access-retry" to="/">
          Voltar ao início
        </Link>
      </section>
    );
  }

  return children;
}
