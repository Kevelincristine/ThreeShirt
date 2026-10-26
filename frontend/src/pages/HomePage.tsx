import { Alert } from "../components/feedback/Alert";
import { Loading } from "../components/feedback/Loading";
import { Link } from "react-router-dom";
import { useAuth } from "../app/AuthContext";

export function HomePage() {
  const { state, refreshSession } = useAuth();

  return (
    <section aria-labelledby="home-title" className="paper-panel">
      <p className="eyebrow">ThreeShirt · um projeto da turma</p>
      <h1 id="home-title">Um espaço para a turma criar junto.</h1>
      {state.status === "loading" && (
        <Loading label="Verificando sua sessão..." />
      )}
      {state.status === "anonymous" && (
        <>
          <p>Entre com seu e-mail escolar para continuar.</p>
          <Link className="button button--primary login-home-link" to="/login">
            Entrar como estudante
          </Link>
          <Alert variant="info">
            Entre para responder à pesquisa, enviar propostas, votar e acompanhar os resultados.
          </Alert>
        </>
      )}
      {state.status === "authenticated" && (
        <>
          <p>
            Boas-vindas! Você está autenticado como{" "}
            {state.user.role === "student"
              ? "estudante"
              : "administrador"}
            .
          </p>
          {state.user.role === "student" ? (
            <Link
              className="button button--primary login-home-link"
              to="/student"
            >
              Abrir painel do estudante
            </Link>
          ) : (
            <>
              <Link
                className="button button--primary login-home-link"
                to="/student"
              >
                Abrir painel do estudante
              </Link>
              <Link
                className="button button--primary login-home-link"
                to="/admin"
              >
                Abrir painel administrativo
              </Link>
              <Alert variant="info">
                A área administrativa está disponível para administradores autorizados.
              </Alert>
            </>
          )}
        </>
      )}
      {state.status === "error" && (
        <>
          <Alert variant="error">
            Não foi possível verificar sua sessão. Tente novamente.
          </Alert>
          <button
            className="button button--primary"
            onClick={() => void refreshSession()}
            type="button"
          >
            Tentar novamente
          </button>
        </>
      )}
    </section>
  );
}
