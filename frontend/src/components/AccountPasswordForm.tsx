import { useState, type FormEvent } from "react";
import { Alert } from "./feedback/Alert";
import { changePassword } from "../services/api";

export function AccountPasswordForm() {
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [passwordConfirmation, setPasswordConfirmation] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [pending, setPending] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending) return;
    if (newPassword !== passwordConfirmation) {
      setError("As senhas não coincidem.");
      return;
    }

    setPending(true);
    setError("");
    setNotice("");
    try {
      const response = await changePassword(
        currentPassword,
        newPassword,
        passwordConfirmation,
      );
      setNotice(response.message);
      setCurrentPassword("");
      setNewPassword("");
      setPasswordConfirmation("");
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "Não foi possível alterar a senha.",
      );
    } finally {
      setPending(false);
    }
  }

  return (
    <section className="admin-section account-security">
      <h2>Segurança da conta</h2>
      <form className="admin-form" onSubmit={(event) => void handleSubmit(event)}>
        <h3>Alterar minha senha</h3>
        {error && <Alert variant="error">{error}</Alert>}
        {notice && <Alert variant="success">{notice}</Alert>}
        <div className="form-field">
          <label htmlFor="current-account-password">Senha atual</label>
          <input
            autoComplete="current-password"
            id="current-account-password"
            maxLength={1024}
            onChange={(event) => setCurrentPassword(event.target.value)}
            required
            type="password"
            value={currentPassword}
          />
        </div>
        <div className="form-field">
          <label htmlFor="new-account-password">Nova senha</label>
          <input
            autoComplete="new-password"
            id="new-account-password"
            maxLength={1024}
            onChange={(event) => setNewPassword(event.target.value)}
            required
            type="password"
            value={newPassword}
          />
        </div>
        <div className="form-field">
          <label htmlFor="confirm-account-password">Confirmar nova senha</label>
          <input
            autoComplete="new-password"
            id="confirm-account-password"
            maxLength={1024}
            onChange={(event) => setPasswordConfirmation(event.target.value)}
            required
            type="password"
            value={passwordConfirmation}
          />
        </div>
        <button
          className="button button--primary"
          disabled={pending}
          type="submit"
        >
          {pending ? "Alterando..." : "Alterar senha"}
        </button>
      </form>
    </section>
  );
}
