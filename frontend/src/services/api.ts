import type {
  AdminDashboardResponse,
  AdminMaintenanceAction,
  AdminMaintenanceOperation,
  AdminMaintenancePreview,
  AdminMaintenanceResult,
  AdminProposal,
  AdminUser,
  ApiMessageResponse,
  AuthenticatedSession,
  LoginCodeRequestResponse,
  ProposalStatus,
  SessionResponse,
  StudentDashboardResponse,
  StudentPreferences,
  StudentPreferencesResponse,
  StudentProposal,
  StudentResultsResponse,
  StudentSubmission,
} from "../types/api";
import { isLoginCodeRequestState } from "./loginCodeRequest.ts";

const API_BASE_URL = import.meta.env?.VITE_API_BASE_URL?.trim() ?? "";
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isCountRecord(value: unknown): value is Record<string, number> {
  return (
    isRecord(value) &&
    Object.values(value).every(
      (count) =>
        typeof count === "number" &&
        Number.isInteger(count) &&
        count >= 0,
    )
  );
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
      (role === "student" || role === "student_admin" || role === "admin")
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
    typeof data.proposal_submission_open === "boolean" &&
    typeof data.voting_open === "boolean" &&
    typeof data.results_release_authorized === "boolean" &&
    typeof data.has_preferences === "boolean" &&
    typeof data.has_voted === "boolean"
  ) {
    return {
      survey_open: data.survey_open,
      proposal_submission_open: data.proposal_submission_open,
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

const STUDENT_COLORS = new Set([
    "RED",
    "BLUE",
    "YELLOW",
    "GREEN",
    "ORANGE",
    "PURPLE",
    "PINK",
    "BROWN",
    "BLACK",
    "WHITE",
]);

function isStringArray(value: unknown): value is string[] {
  return (
    Array.isArray(value) &&
    value.every((item: unknown) => typeof item === "string")
  );
}

function isStudentPreferences(value: unknown): value is StudentPreferences {
    return (
      isRecord(value) &&
      (value.base_color === "BLACK" || value.base_color === "WHITE") &&
      typeof value.related_to_course === "boolean" &&
      typeof value.wants_animal === "boolean" &&
      typeof value.preferred_animal === "string" &&
      isStringArray(value.preferred_colors) &&
      value.preferred_colors.every((color) => STUDENT_COLORS.has(color)) &&
      typeof value.wants_collar === "boolean" &&
      typeof value.wants_phrase === "boolean" &&
      typeof value.intends_to_buy === "boolean"
    );
}

function getStudentPreferencesResponse(
  data: unknown,
): StudentPreferencesResponse {
    if (
      isRecord(data) &&
      data.has_preferences === false &&
      data.preferences === null
    ) {
      return { has_preferences: false, preferences: null };
    }
    if (
      isRecord(data) &&
      data.has_preferences === true &&
      isStudentPreferences(data.preferences)
    ) {
      return { has_preferences: true, preferences: data.preferences };
    }
    throw new ApiError("As preferências recebidas são inválidas.", 200);
}

function getApprovedProposals(data: unknown): StudentProposal[] {
    if (!isRecord(data) || !Array.isArray(data.proposals)) {
      throw new ApiError("A galeria recebida é inválida.", 200);
    }
    return data.proposals.map((proposal) => {
      if (
        !isRecord(proposal) ||
        typeof proposal.id !== "number" ||
        typeof proposal.title !== "string" ||
        typeof proposal.description !== "string" ||
        typeof proposal.image_url !== "string" ||
        (proposal.base_color !== "BLACK" && proposal.base_color !== "WHITE") ||
        typeof proposal.related_to_course !== "boolean" ||
        typeof proposal.animal !== "string" ||
        !isStringArray(proposal.additional_colors) ||
        typeof proposal.has_collar !== "boolean" ||
        typeof proposal.has_phrase !== "boolean" ||
        typeof proposal.phrase_text !== "string"
      ) {
        throw new ApiError("Uma proposta recebida pela galeria é inválida.", 200);
      }
      return {
        id: proposal.id,
        title: proposal.title,
        description: proposal.description,
        image_url: proposal.image_url,
        base_color: proposal.base_color,
        related_to_course: proposal.related_to_course,
        animal: proposal.animal,
        additional_colors: proposal.additional_colors,
        has_collar: proposal.has_collar,
        has_phrase: proposal.has_phrase,
        phrase_text: proposal.phrase_text,
      };
    });
}

function getProposalStatus(value: unknown): value is ProposalStatus {
    return value === "PENDING" || value === "APPROVED" || value === "REJECTED";
}

function parseStudentSubmissions(data: unknown): StudentSubmission[] {
    if (!isRecord(data) || !Array.isArray(data.proposals)) {
      throw new ApiError("O acompanhamento de propostas é inválido.", 200);
    }
    return data.proposals.map((proposal) => {
      if (
        !isRecord(proposal) ||
        typeof proposal.id !== "number" ||
        typeof proposal.title !== "string" ||
        typeof proposal.description !== "string" ||
        !getProposalStatus(proposal.status) ||
        typeof proposal.created_at !== "string"
      ) {
        throw new ApiError("Uma proposta enviada possui dados inválidos.", 200);
      }
      return {
        id: proposal.id,
        title: proposal.title,
        description: proposal.description,
        status: proposal.status,
        created_at: proposal.created_at,
      };
    });
}

function getNumberOrNull(value: unknown): number | null {
  if (value === null) return null;
  const parsed = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(parsed)) {
    throw new ApiError("Um valor estatístico recebido é inválido.", 200);
  }
  return parsed;
}

function parseStudentResults(data: unknown): StudentResultsResponse {
    if (
      !isRecord(data) ||
      (data.status !== "complete" && data.status !== "no_votes") ||
      typeof data.total_votes !== "number" ||
      !Array.isArray(data.proposals) ||
      !Array.isArray(data.winners) ||
      typeof data.is_tie !== "boolean" ||
      (data.compatibility_status !== "available" &&
        data.compatibility_status !== "insufficient_data") ||
      !Array.isArray(data.compatibility_winners) ||
      typeof data.compatibility_is_tie !== "boolean"
    ) {
      throw new ApiError("Os resultados recebidos são inválidos.", 200);
    }

    const proposals = data.proposals.map((item) => {
      if (
        !isRecord(item) ||
        typeof item.proposal_id !== "number" ||
        typeof item.title !== "string" ||
        typeof item.vote_count !== "number" ||
        typeof item.valid_assessments !== "number"
      ) {
        throw new ApiError("Uma estatística recebida é inválida.", 200);
      }
      return {
        proposal_id: item.proposal_id,
        title: item.title,
        vote_count: item.vote_count,
        percentage: getNumberOrNull(item.percentage),
        compatibility: getNumberOrNull(item.compatibility),
        valid_assessments: item.valid_assessments,
      };
    });
    const winners = data.winners.map((item) => {
      if (
        !isRecord(item) ||
        typeof item.proposal_id !== "number" ||
        typeof item.title !== "string" ||
        typeof item.vote_count !== "number"
      ) {
        throw new ApiError("A lista de vencedoras recebida é inválida.", 200);
      }
      return {
        proposal_id: item.proposal_id,
        title: item.title,
        vote_count: item.vote_count,
        percentage: getNumberOrNull(item.percentage),
      };
    });
    const compatibilityWinners = data.compatibility_winners.map((item) => {
      if (
        !isRecord(item) ||
        typeof item.proposal_id !== "number" ||
        typeof item.title !== "string" ||
        typeof item.valid_assessments !== "number" ||
        (item.status !== "available" && item.status !== "insufficient_data")
      ) {
        throw new ApiError("A compatibilidade recebida é inválida.", 200);
      }
      const status: "available" | "insufficient_data" =
        item.status === "available" ? "available" : "insufficient_data";
      return {
        proposal_id: item.proposal_id,
        title: item.title,
        valid_assessments: item.valid_assessments,
        average_compatibility: getNumberOrNull(item.average_compatibility),
        status,
      };
    });

    return {
      status: data.status,
      total_votes: data.total_votes,
      proposals,
      winners,
      is_tie: data.is_tie,
      compatibility_status: data.compatibility_status === "available"
        ? "available"
        : "insufficient_data",
      compatibility_winners: compatibilityWinners,
      compatibility_is_tie: data.compatibility_is_tie,
    };
}

function getAdminDashboardResponse(data: unknown): AdminDashboardResponse {
  if (
    !isRecord(data) ||
    typeof data.survey_open !== "boolean" ||
    typeof data.proposal_submission_open !== "boolean" ||
    typeof data.voting_open !== "boolean" ||
    typeof data.results_release_authorized !== "boolean" ||
    !isRecord(data.counts) ||
    !isRecord(data.progress)
  ) {
    throw new ApiError("O painel administrativo retornou dados inválidos.", 200);
  }

  const counts = data.counts as Record<string, unknown>;
  const progress = data.progress as Record<string, unknown>;
  return {
    survey_open: data.survey_open,
    proposal_submission_open: data.proposal_submission_open,
    voting_open: data.voting_open,
    results_release_authorized: data.results_release_authorized,
    counts: {
      students_total: getNumberOrNull(counts.students_total) ?? 0,
      users_total: getNumberOrNull(counts.users_total) ?? 0,
      proposals_pending: getNumberOrNull(counts.proposals_pending) ?? 0,
      proposals_approved: getNumberOrNull(counts.proposals_approved) ?? 0,
      proposals_rejected: getNumberOrNull(counts.proposals_rejected) ?? 0,
      preferences_responses: getNumberOrNull(counts.preferences_responses) ?? 0,
      votes_cast: getNumberOrNull(counts.votes_cast) ?? 0,
      admins_total: getNumberOrNull(counts.admins_total) ?? 0,
    },
    progress: {
      survey_participation: getNumberOrNull(progress.survey_participation),
      survey_completed: getNumberOrNull(progress.survey_completed) ?? 0,
    },
  };
}

function parseAdminProposals(data: unknown): AdminProposal[] {
  if (!isRecord(data) || !Array.isArray(data.proposals)) {
    throw new ApiError("A lista de propostas administrativas é inválida.", 200);
  }

  return data.proposals.map((proposal) => {
    if (
      !isRecord(proposal) ||
      typeof proposal.id !== "number" ||
      typeof proposal.title !== "string" ||
      typeof proposal.description !== "string" ||
      !getProposalStatus(proposal.status) ||
      (proposal.base_color !== "BLACK" && proposal.base_color !== "WHITE") ||
      typeof proposal.animal !== "string" ||
      !isStringArray(proposal.additional_colors) ||
      typeof proposal.has_collar !== "boolean" ||
      typeof proposal.has_phrase !== "boolean" ||
      typeof proposal.phrase_text !== "string" ||
      typeof proposal.created_at !== "string" ||
      typeof proposal.updated_at !== "string" ||
      (proposal.reviewed_at !== null && typeof proposal.reviewed_at !== "string") ||
      typeof proposal.rejection_reason !== "string" ||
      typeof proposal.image_url !== "string" ||
      !isRecord(proposal.author) ||
      typeof proposal.author.id !== "number" ||
      typeof proposal.author.email !== "string" ||
      typeof proposal.author.full_name !== "string"
    ) {
      throw new ApiError("Uma proposta administrativa possui dados inválidos.", 200);
    }

    return {
      id: proposal.id,
      title: proposal.title,
      description: proposal.description,
      status: proposal.status,
      base_color: proposal.base_color,
      animal: proposal.animal,
      additional_colors: proposal.additional_colors,
      has_collar: proposal.has_collar,
      has_phrase: proposal.has_phrase,
      phrase_text: proposal.phrase_text,
      created_at: proposal.created_at,
      updated_at: proposal.updated_at,
      reviewed_at: proposal.reviewed_at ?? null,
      rejection_reason: proposal.rejection_reason,
      image_url: proposal.image_url,
      author: {
        id: proposal.author.id,
        email: proposal.author.email,
        full_name: proposal.author.full_name,
      },
      reviewed_by: isRecord(proposal.reviewed_by)
        ? {
            id: proposal.reviewed_by.id as number,
            email: proposal.reviewed_by.email as string,
            full_name: proposal.reviewed_by.full_name as string,
          }
        : null,
    };
  });
}

function parseAdminUsers(data: unknown): AdminUser[] {
  if (!isRecord(data) || !Array.isArray(data.users)) {
    throw new ApiError("A lista de usuários administrativos é inválida.", 200);
  }

  return data.users.map((user) => {
    if (
      !isRecord(user) ||
      typeof user.id !== "number" ||
      typeof user.email !== "string" ||
      typeof user.full_name !== "string" ||
      (user.role !== "STUDENT" &&
        user.role !== "STUDENT_ADMIN" &&
        user.role !== "ADMIN") ||
      typeof user.is_active !== "boolean" ||
      typeof user.is_staff !== "boolean"
    ) {
      throw new ApiError("Um usuário administrativo possui dados inválidos.", 200);
    }
    return {
      id: user.id,
      email: user.email,
      full_name: user.full_name,
      role: user.role,
      is_active: user.is_active,
      is_staff: user.is_staff,
    };
  });
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
    const fieldErrors = isRecord(body.fields)
      ? Object.entries(body.fields).flatMap(([field, value]) => {
          const messages = Array.isArray(value) ? value : [value];
          return messages
            .filter((message): message is string =>
              typeof message === "string" && Boolean(message.trim()),
            )
            .map((message) => `${field}: ${message.trim()}`);
        })
      : [];
    let message = "";
    for (const key of ["error", "message", "detail"]) {
      const value = body[key];
      if (typeof value === "string" && value.trim()) {
        message = value.trim();
        break;
      }
    }
    if (fieldErrors.length > 0) {
      return [message || "Verifique os dados enviados.", ...fieldErrors]
        .join(" ")
        .slice(0, 500);
    }
    if (message) return message.slice(0, 300);
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
    if (
      response.status === 401 &&
      path.startsWith("/api/election/student/") &&
      typeof window !== "undefined"
    ) {
      window.dispatchEvent(new Event("threeshirt:session-expired"));
    }
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

export async function getStudentPreferences(
  signal?: AbortSignal,
): Promise<StudentPreferencesResponse> {
  const data = await apiRequest<unknown>(
    "/api/election/student/preferences/",
    { signal },
  );
  return getStudentPreferencesResponse(data);
}

export async function saveStudentPreferences(
  preferences: StudentPreferences,
): Promise<ApiMessageResponse & StudentPreferencesResponse> {
  const data = await apiRequest<unknown>(
    "/api/election/student/preferences/",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(preferences),
    },
  );
  if (!isRecord(data) || typeof data.message !== "string") {
    throw new ApiError("A resposta ao salvar preferências é inválida.", 200);
  }
  return {
    ...getStudentPreferencesResponse(data),
    message: data.message,
  };
}

export async function getApprovedStudentProposals(
  signal?: AbortSignal,
): Promise<StudentProposal[]> {
  const data = await apiRequest<unknown>(
    "/api/election/student/proposals/",
    { signal },
  );
  return getApprovedProposals(data);
}

export async function getStudentSubmissions(
  signal?: AbortSignal,
): Promise<StudentSubmission[]> {
  const data = await apiRequest<unknown>(
    "/api/election/student/submissions/",
    { signal },
  );
  return parseStudentSubmissions(data);
}

export async function submitStudentProposal(
  payload: FormData,
): Promise<{ id: number; title: string; description: string; status: ProposalStatus }> {
  const data = await apiRequest<unknown>(
    "/api/election/student/submissions/",
    { method: "POST", body: payload },
  );
  if (
    !isRecord(data) ||
    !isRecord(data.proposal) ||
    typeof data.proposal.id !== "number" ||
    typeof data.proposal.title !== "string" ||
    typeof data.proposal.description !== "string" ||
    !getProposalStatus(data.proposal.status)
  ) {
    throw new ApiError("A resposta ao enviar a proposta é inválida.", 201);
  }
  return {
    id: data.proposal.id,
    title: data.proposal.title,
    description: data.proposal.description,
    status: data.proposal.status,
  };
}

export async function castStudentVote(proposalId: number): Promise<void> {
  const data = await apiRequest<unknown>(
    "/api/election/student/vote/",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ proposal_id: proposalId }),
    },
  );
  if (!isRecord(data) || data.has_voted !== true) {
    throw new ApiError("O servidor não confirmou o registro do voto.", 200);
  }
}

export async function getStudentResults(
  signal?: AbortSignal,
): Promise<StudentResultsResponse> {
  const data = await apiRequest<unknown>(
    "/api/election/student/results/",
    { signal },
  );
  return parseStudentResults(data);
}

export async function getAdminDashboard(
  signal?: AbortSignal,
): Promise<AdminDashboardResponse> {
  const data = await apiRequest<unknown>(
    "/api/election/admin/dashboard/",
    { signal },
  );
  return getAdminDashboardResponse(data);
}

export async function getAdminProposals(
  signal?: AbortSignal,
): Promise<AdminProposal[]> {
  const data = await apiRequest<unknown>(
    "/api/election/admin/proposals/",
    { signal },
  );
  return parseAdminProposals(data);
}

export async function reviewAdminProposal(
  proposalId: number,
  decision: "approve" | "reject",
  reason = "",
): Promise<{ message: string }> {
  const data = await apiRequest<unknown>(
    `/api/election/admin/proposals/${proposalId}/review/`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision, reason }),
    },
  );
  if (!isRecord(data) || typeof data.message !== "string") {
    throw new ApiError("A resposta da revisão administrativa é inválida.", 200);
  }
  return { message: data.message };
}

export async function getAdminStages(
  signal?: AbortSignal,
): Promise<{ survey_open: boolean; proposal_submission_open: boolean; voting_open: boolean; results_release_authorized: boolean }> {
  const data = await apiRequest<unknown>(
    "/api/election/admin/stages/",
    { signal },
  );
  if (
    !isRecord(data) ||
    typeof data.survey_open !== "boolean" ||
    typeof data.proposal_submission_open !== "boolean" ||
    typeof data.voting_open !== "boolean" ||
    typeof data.results_release_authorized !== "boolean"
  ) {
    throw new ApiError("O estado das etapas administrativas é inválido.", 200);
  }
  return {
    survey_open: data.survey_open,
    proposal_submission_open: data.proposal_submission_open,
    voting_open: data.voting_open,
    results_release_authorized: data.results_release_authorized,
  };
}

export async function updateAdminStage(
  stage: "survey" | "proposal_submission" | "voting" | "results_release",
  action: "open" | "close",
): Promise<{ message: string }> {
  const data = await apiRequest<unknown>(
    "/api/election/admin/stages/",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stage, action }),
    },
  );
  if (!isRecord(data) || typeof data.message !== "string") {
    throw new ApiError("A resposta ao atualizar a etapa é inválida.", 200);
  }
  return { message: data.message };
}

export async function getAdminUsers(
  signal?: AbortSignal,
): Promise<AdminUser[]> {
  const data = await apiRequest<unknown>(
    "/api/election/admin/users/",
    { signal },
  );
  return parseAdminUsers(data);
}

export async function updateAdminUserRole(
  userId: number,
  role: "ADMIN" | "STUDENT_ADMIN" | "STUDENT",
): Promise<{ message: string }> {
  const data = await apiRequest<unknown>(
    `/api/election/admin/users/${userId}/role/`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role }),
    },
  );
  if (!isRecord(data) || typeof data.message !== "string") {
    throw new ApiError("A resposta ao atualizar a permissão é inválida.", 200);
  }
  return { message: data.message };
}

export async function updateAdminUserStatus(
  userId: number,
  isActive: boolean,
): Promise<{ message: string }> {
  const data = await apiRequest<unknown>(
    `/api/election/admin/users/${userId}/status/`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ is_active: isActive }),
    },
  );
  if (!isRecord(data) || typeof data.message !== "string") {
    throw new ApiError("A resposta ao atualizar o acesso é inválida.", 200);
  }
  return { message: data.message };
}

export async function previewAdminMaintenance(
  action: AdminMaintenanceAction,
  targetId?: number,
): Promise<AdminMaintenancePreview> {
  const data = await apiRequest<unknown>(
    "/api/election/admin/maintenance/preview/",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, target_id: targetId }),
    },
  );
  const validOperation = (
    value: unknown,
  ): value is AdminMaintenanceOperation =>
    value === "DELETE_USER" ||
    value === "DELETE_PROPOSAL" ||
    value === "RESET_ELECTION";
  if (
    !isRecord(data) ||
    typeof data.challenge_token !== "string" ||
    typeof data.expires_at !== "string" ||
    !validOperation(data.action) ||
    !isRecord(data.target) ||
    typeof data.target.label !== "string" ||
    !isCountRecord(data.summary) ||
    typeof data.warning !== "string" ||
    (data.target.id !== undefined &&
      (typeof data.target.id !== "number" || !Number.isInteger(data.target.id))) ||
    (data.target.email !== undefined && typeof data.target.email !== "string") ||
    (data.target.author !== undefined && typeof data.target.author !== "string") ||
    (data.target.status !== undefined && !getProposalStatus(data.target.status))
  ) {
    throw new ApiError("A prévia de manutenção recebida é inválida.", 200);
  }

  let restoredSettings: AdminMaintenancePreview["restored_settings"];
  if (data.restored_settings !== undefined) {
    const restored = data.restored_settings;
    if (
      !isRecord(restored) ||
      typeof restored.survey_open !== "boolean" ||
      typeof restored.proposal_submission_open !== "boolean" ||
      typeof restored.voting_open !== "boolean" ||
      typeof restored.results_release_authorized !== "boolean" ||
      typeof restored.compatibility_weight !== "number"
    ) {
      throw new ApiError("O estado inicial da eleição recebido é inválido.", 200);
    }
    restoredSettings = {
      survey_open: restored.survey_open,
      proposal_submission_open: restored.proposal_submission_open,
      voting_open: restored.voting_open,
      results_release_authorized: restored.results_release_authorized,
      compatibility_weight: restored.compatibility_weight,
    };
  }

  return {
    challenge_token: data.challenge_token,
    expires_at: data.expires_at,
    action: data.action,
    target: {
      id: typeof data.target.id === "number" ? data.target.id : undefined,
      label: data.target.label,
      email: typeof data.target.email === "string" ? data.target.email : undefined,
      status: getProposalStatus(data.target.status) ? data.target.status : undefined,
      author: typeof data.target.author === "string" ? data.target.author : undefined,
    },
    summary: data.summary,
    warning: data.warning,
    restored_settings: restoredSettings,
  };
}

export async function confirmAdminMaintenance(
  challengeToken: string,
  confirmationPhrase?: string,
): Promise<AdminMaintenanceResult> {
  const data = await apiRequest<unknown>(
    "/api/election/admin/maintenance/confirm/",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        challenge_token: challengeToken,
        confirmation_phrase: confirmationPhrase,
      }),
    },
  );
  if (
    !isRecord(data) ||
    typeof data.message !== "string" ||
    !isCountRecord(data.affected) ||
    typeof data.warning !== "string" ||
    typeof data.file_cleanup_pending !== "boolean"
  ) {
    throw new ApiError("A resposta à manutenção é inválida.", 200);
  }
  return {
    message: data.message,
    affected: data.affected,
    warning: data.warning,
    file_cleanup_pending: data.file_cleanup_pending,
  };
}
