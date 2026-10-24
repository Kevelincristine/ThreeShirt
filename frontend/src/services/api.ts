import type {
  ApiMessageResponse,
  AuthenticatedSession,
  LoginCodeRequestResponse,
  SessionResponse,
  StudentDashboardResponse,
} from "../types/api";
import { isLoginCodeRequestState } from "./loginCodeRequest";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL?.trim() ?? "";
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function getMessageResponse(data: unknown, status: number): ApiMessageResponse {
  if (
    isRecord(data) &&
    typeof data.message === "string" &&
    data.message.trim()
  ) {
    return { message: data.message.trim() };
  }
  throw new ApiError(
    "O servidor retornou uma resposta inesperada.",
    status,
  );
}

function getLoginCodeRequestResponse(
  data: unknown,
  status: number,
): LoginCodeRequestResponse {
  if (
    isRecord(data) &&
    isLoginCodeRequestState(data.state) &&
    typeof data.message === "string" &&
    data.message.trim()
  ) {
    return {
      state: data.state,
      message: data.message.trim(),
    };
  }
  throw new ApiError(
    "O servidor retornou uma resposta inesperada.",
    status,
  );
}

function getSessionResponse(data: unknown): SessionResponse {
  if (isRecord(data) && data.authenticated === false) {
    return { authenticated: false };
  }

  if (isRecord(data) && data.authenticated === true && isRecord(data.user)) {
    const { id, email, role } = data.user;
    if (
      typeof id === "number" &&
      Number.isInteger(id) &&
      typeof email === "string" &&
      (role === "student" || role === "admin")
    ) {
      const session: AuthenticatedSession = {
        authenticated: true,
        user: { id, email, role },
      };
      return session;
    }
  }

  throw new ApiError(
    "O servidor retornou uma resposta inesperada.",
    200,
  );
}

function getStudentDashboardResponse(
  data: unknown,
): StudentDashboardResponse {
  if (
    isRecord(data) &&
    typeof data.survey_open === "boolean" &&
    typeof data.voting_open === "boolean" &&
    typeof data.results_release_authorized === "boolean" &&
    typeof data.has_preferences === "boolean" &&
    typeof data.has_voted === "boolean"
  ) {
    return {
      survey_open: data.survey_open,
      voting_open: data.voting_open,
      results_release_authorized: data.results_release_authorized,
      has_preferences: data.has_preferences,
      has_voted: data.has_voted,
    };
  }

  throw new ApiError(
    "O servidor retornou um estado de painel inesperado.",
    200,
  );
}

function getCsrfToken(): string | null {
  const cookie = document.cookie
    .split("; ")
    .find((part) => part.startsWith("csrftoken="))
    ?.slice("csrftoken=".length);

  if (cookie) return decodeURIComponent(cookie);
  return document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')
    ?.content ?? null;
}

function getErrorMessage(data: unknown, status: number): string {
  if (typeof data === "object" && data !== null) {
    const body = data as Record<string, unknown>;
    for (const key of ["error", "message", "detail"]) {
      const value = body[key];
      if (typeof value === "string" && value.trim()) {
        return value.trim().slice(0, 300);
      }
    }
  }

  if (status === 403) {
    return "Solicitação recusada. Verifique sua sessão e a proteção CSRF.";
  }
  if (status >= 500) {
    return "O servidor não conseguiu concluir a solicitação. Tente novamente.";
  }
  return "Não foi possível concluir a solicitação.";
}

async function readResponse(response: Response): Promise<unknown> {
  const responseText = await response.text();
  if (!responseText) return null;

  const contentType = response.headers.get("content-type") ?? "";
  if (
    !contentType.includes("application/json") &&
    !contentType.includes("+json")
  ) {
    return null;
  }

  try {
    return JSON.parse(responseText) as unknown;
  } catch (error) {
    if (error instanceof SyntaxError) {
      throw new ApiError(
        "O servidor retornou uma resposta inválida.",
        response.status,
      );
    }
    throw error;
  }
}

export async function apiRequest<T>(
  path: string,
  init: RequestInit = {},
  csrfTokenOverride?: string,
): Promise<T> {
  if (!path.startsWith("/") || path.startsWith("//")) {
    throw new TypeError("O caminho da API deve ser relativo à origem.");
  }

  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");

  if (!SAFE_METHODS.has(method)) {
    const csrfToken = csrfTokenOverride ?? getCsrfToken();
    if (!csrfToken) {
      throw new ApiError(
        "Token CSRF indisponível. O backend precisa inicializá-lo antes desta ação.",
        0,
      );
    }
    headers.set("X-CSRFToken", csrfToken);
  }

  if (
    init.body instanceof URLSearchParams &&
    !headers.has("Content-Type")
  ) {
    headers.set("Content-Type", "application/x-www-form-urlencoded;charset=UTF-8");
  }

  const requestUrl = new URL(
    path,
    API_BASE_URL || window.location.origin,
  );

  let response: Response;
  try {
    response = await fetch(requestUrl, {
      ...init,
      method,
      headers,
      credentials: "include",
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw error;
    }
    if (error instanceof TypeError) {
      throw new ApiError(
        "Não foi possível conectar ao servidor. Verifique sua conexão e tente novamente.",
        0,
      );
    }
    throw error;
  }

  const data = await readResponse(response);
  if (!response.ok) {
    throw new ApiError(getErrorMessage(data, response.status), response.status);
  }

  return data as T;
}

async function initializeCsrf(): Promise<string> {
  const data = await apiRequest<unknown>("/api/accounts/csrf/");
  if (isRecord(data) && typeof data.csrfToken === "string" && data.csrfToken) {
    return data.csrfToken;
  }
  throw new ApiError(
    "O servidor não forneceu um token CSRF válido.",
    200,
  );
}

export async function requestLoginCode(
  email: string,
): Promise<LoginCodeRequestResponse> {
  const csrfToken = await initializeCsrf();
  const data = await apiRequest<unknown>(
    "/api/accounts/request-code/",
    {
      method: "POST",
      body: new URLSearchParams({ email }),
    },
    csrfToken,
  );
  return getLoginCodeRequestResponse(data, 200);
}

export async function verifyLoginCode(
  email: string,
  code: string,
): Promise<ApiMessageResponse> {
  const csrfToken = await initializeCsrf();
  const data = await apiRequest<unknown>(
    "/api/accounts/verify-code/",
    {
      method: "POST",
      body: new URLSearchParams({ email, code }),
    },
    csrfToken,
  );
  return getMessageResponse(data, 200);
}

export async function getSession(): Promise<SessionResponse> {
  const data = await apiRequest<unknown>("/api/accounts/session/");
  return getSessionResponse(data);
}

export async function logoutSession(): Promise<ApiMessageResponse> {
  const csrfToken = await initializeCsrf();
  const data = await apiRequest<unknown>(
    "/api/accounts/logout/",
    { method: "POST" },
    csrfToken,
  );
  if (!isRecord(data) || data.authenticated !== false) {
    throw new ApiError(
      "O servidor não confirmou o encerramento da sessão.",
      200,
    );
  }
  return getMessageResponse(data, 200);
}

export async function getStudentDashboard(
  signal?: AbortSignal,
): Promise<StudentDashboardResponse> {
  const data = await apiRequest<unknown>(
    "/api/election/student/dashboard/",
    { signal },
  );
  return getStudentDashboardResponse(data);
}
