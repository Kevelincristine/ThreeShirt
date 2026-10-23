import { useRef, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { Alert } from "../components/feedback/Alert";
import { ApiError, requestLoginCode, verifyLoginCode } from "../services/api";

type LoginStep = "email" | "code" | "complete";
type LoginAction = "request" | "verify" | null;

export function StudentLoginPage() {
  const [step, setStep] = useState<LoginStep>("email");
  const [action, setAction] = useState<LoginAction>(null);
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const actionInProgress = useRef(false);

  function describeError(reason: unknown): string {
    if (reason instanceof ApiError) return reason.message;
    return "Não foi possível concluir a solicitação. Tente novamente.";
  }

  async function sendCode(event?: FormEvent<HTMLFormElement>) {
    event?.preventDefault();
    if (actionInProgress.current) return;

    actionInProgress.current = true;
    setAction("request");
    setError("");
    setNotice("");
    try {
      const response = await requestLoginCode(email.trim().toLowerCase());
      setStep("code");
      setCode("");
      setNotice(response.message);
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      actionInProgress.current = false;
      setAction(null);
    }
  }

  async function verifyCode(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (actionInProgress.current) return;

    actionInProgress.current = true;
    setAction("verify");
    setError("");
    setNotice("");
    try {
      const response = await verifyLoginCode(
        email.trim().toLowerCase(),
        code,
      );
      setStep("complete");
      setNotice(response.message);
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      actionInProgress.current = false;
      setAction(null);
    }
  }

  return (
    <section
      aria-labelledby="student-login-title"
      className="paper-panel login-panel"
    >
      <p className="eyebrow">Sua jornada começa aqui</p>
      <h1 id="student-login-title">Acesse o ThreeShirt</h1>
      <p className="login-intro">
        Use seu e-mail escolar para receber um código temporário de acesso.
      </p>

      {error && <Alert variant="error">{error}</Alert>}
      {notice && step !== "complete" && (
        <Alert variant="success">{notice}</Alert>
      )}

      {step === "email" && (
        <form className="login-form" onSubmit={(event) => void sendCode(event)}>
          <div className="form-field">
            <label htmlFor="student-email">E-mail escolar</label>
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
          <button
            className="button button--primary"
            disabled={action !== null}
            type="submit"
          >
            {action === "request" ? "Enviando..." : "Solicitar código"}
          </button>
        </form>
      )}

      {step === "code" && (
        <>
          <p className="login-email-context">
            Código solicitado para <strong>{email}</strong>
          </p>
          <form className="login-form" onSubmit={verifyCode}>
            <div className="form-field">
              <label htmlFor="verification-code">Código de verificação</label>
              <input
                autoComplete="one-time-code"
                autoFocus
                aria-describedby="verification-code-hint"
                id="verification-code"
                inputMode="numeric"
                maxLength={6}
                name="code"
                onChange={(event) =>
                  setCode(event.target.value.replace(/\D/g, "").slice(0, 6))
                }
                pattern="[0-9]{6}"
                required
                type="text"
                value={code}
              />
              <p className="form-hint" id="verification-code-hint">
                Digite os seis números recebidos no seu e-mail.
              </p>
            </div>
            <button
              className="button button--primary"
              disabled={action !== null}
              type="submit"
            >
              {action === "verify" ? "Verificando..." : "Verificar código"}
            </button>
          </form>
          <div className="login-secondary-actions">
            <button
              className="text-button"
              disabled={action !== null}
              onClick={() => void sendCode()}
              type="button"
            >
              {action === "request" ? "Enviando..." : "Enviar outro código"}
            </button>
            <button
              className="text-button"
              disabled={action !== null}
              onClick={() => {
                setStep("email");
                setCode("");
                setError("");
                setNotice("");
              }}
              type="button"
            >
              Alterar e-mail
            </button>
          </div>
        </>
      )}

      {step === "complete" && (
        <div className="login-complete">
          <Alert variant="success">{notice}</Alert>
          <p>
            Seu acesso foi confirmado pelo servidor. Você pode seguir para a
            página inicial.
          </p>
          <Link className="button button--primary" to="/">
            Ir para o início
          </Link>
        </div>
      )}
    </section>
  );
}
