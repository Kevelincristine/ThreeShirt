import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Alert } from "../components/feedback/Alert";
import { useAuth } from "../app/AuthContext";
import { ApiError, loginWithPassword, setFirstAccessPassword } from "../services/api";

export function StudentLoginPage({ firstAccess = false }: { firstAccess?: boolean }) {
  const { refreshSession } = useAuth();
  const navigate = useNavigate();
  const [mode, setMode] = useState<"login" | "first-access">(
    firstAccess ? "first-access" : "login",
  );
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [passwordConfirmation, setPasswordConfirmation] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);

  function selectMode(nextMode: "login" | "first-access") {
    setMode(nextMode);
    setPassword("");
    setPasswordConfirmation("");
    setNotice("");
    setError("");
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending) return;
    setPending(true);
    setError("");
    setNotice("");
    try {
      if (mode === "first-access") {
        if (password !== passwordConfirmation) {
          setError("As senhas não coincidem.");
          return;
        }
        const response = await setFirstAccessPassword(
          email.trim(),
          password,
          passwordConfirmation,
        );
        setNotice(response.message);
        setPassword("");
        setPasswordConfirmation("");
        setMode("login");
      } else {
        await loginWithPassword(email.trim(), password);
        const session = await refreshSession();
        if (session.status !== "authenticated") {
          throw new Error("O servidor não confirmou a sessão autenticada.");
        }
        navigate(
          session.user.role === "student" ||
            session.user.role === "student_admin"
            ? "/student"
            : "/admin",
          { replace: true },
        );
      }
    } catch (reason) {
      const message =
        reason instanceof Error
          ? reason.message
          : "Não foi possível concluir a solicitação. Tente novamente.";
      if (mode === "first-access" && reason instanceof ApiError && reason.status === 409) {
        setNotice(message);
        setPassword("");
        setPasswordConfirmation("");
        setMode("login");
      } else {
        setError(message);
      }
    } finally {
      setPending(false);
    }
  }

  return (
    <section
      aria-labelledby="student-login-title"
      className="paper-panel login-panel"
    >
      <p className="eyebrow">
        {mode === "first-access"
          ? "Crie sua senha para entrar"
          : "Sua jornada começa aqui"}
      </p>
      <h1 id="student-login-title">
        {mode === "first-access" ? "Primeiro acesso" : "Acesse o ThreeShirt"}
      </h1>
      {error && <Alert variant="error">{error}</Alert>}
      {notice && <Alert variant="info">{notice}</Alert>}

      <form className="login-form" onSubmit={(event) => void handleSubmit(event)}>
        <div className="form-field">
          <label htmlFor="student-email">Gmail autorizado</label>
          <input
            autoComplete="email"
            id="student-email"
            maxLength={254}
            name="email"
            onChange={(event) => setEmail(event.target.value)}
            required
            type="email"
            value={email}
          />
        </div>
        <div className="form-field">
          <label htmlFor="student-password">
            {mode === "login" ? "Senha" : "Nova senha"}
          </label>
          <input
            autoComplete={mode === "login" ? "current-password" : "new-password"}
            id="student-password"
            maxLength={1024}
            name="password"
            onChange={(event) => setPassword(event.target.value)}
            required
            type="password"
            value={password}
          />
        </div>
        {mode === "first-access" && (
          <div className="form-field">
            <label htmlFor="student-password-confirmation">
              Confirmar nova senha
            </label>
            <input
              autoComplete="new-password"
              id="student-password-confirmation"
              maxLength={1024}
              onChange={(event) => setPasswordConfirmation(event.target.value)}
              required
              type="password"
              value={passwordConfirmation}
            />
          </div>
        )}
        <button
          className="button button--primary"
          disabled={pending}
          type="submit"
        >
          {pending
            ? "Aguarde..."
            : mode === "login"
              ? "Entrar"
              : "Definir senha"}
        </button>
      </form>

      <div className="login-secondary-actions">
        {mode === "login" ? (
          <Link className="text-button" to="/first-access">
            Primeiro acesso? Defina sua senha
          </Link>
        ) : (
          <button
            className="text-button"
            disabled={pending}
            onClick={() => selectMode("login")}
            type="button"
          >
            Voltar ao login
          </button>
        )}
      </div>
    </section>
  );
}
