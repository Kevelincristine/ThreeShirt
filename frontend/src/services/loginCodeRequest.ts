import type {
  LoginCodeRequestResponse,
  LoginCodeRequestState,
} from "../types/api";

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export function isLoginCodeRequestState(
  state: unknown,
): state is LoginCodeRequestState {
  return (
    state === "unauthorized" ||
    state === "account_unavailable" ||
    state === "send_failed" ||
    state === "rate_limited" ||
    state === "invalid_email" ||
    state === "enter_code"
  );
}

export function isLoginCodeAccepted(state: LoginCodeRequestState): boolean {
  return state === "enter_code";
}

export function isEmailSyntaxValid(email: string): boolean {
  return email.length <= 254 && EMAIL_PATTERN.test(email);
}

export async function requestLoginCodeForEmail(
  email: string,
  request: (normalizedEmail: string) => Promise<LoginCodeRequestResponse>,
): Promise<LoginCodeRequestResponse> {
  const normalizedEmail = email.trim().toLowerCase();
  if (!isEmailSyntaxValid(normalizedEmail)) {
    return {
      state: "invalid_email",
      message: "Digite um endereço de e-mail válido.",
    };
  }
  return request(normalizedEmail);
}
