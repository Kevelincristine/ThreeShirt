export interface ApiMessageResponse {
  message: string;
}

export type LoginCodeRequestState =
  | "unauthorized"
  | "account_unavailable"
  | "send_failed"
  | "rate_limited"
  | "invalid_email"
  | "enter_code";

export interface LoginCodeRequestResponse extends ApiMessageResponse {
  state: LoginCodeRequestState;
}

export type SessionRole = "student" | "admin";

export interface SessionUser {
  id: number;
  email: string;
  role: SessionRole;
}

export interface UnauthenticatedSession {
  authenticated: false;
}

export interface AuthenticatedSession {
  authenticated: true;
  user: SessionUser;
}

export type SessionResponse =
  | UnauthenticatedSession
  | AuthenticatedSession;

export interface StudentDashboardResponse {
  survey_open: boolean;
  voting_open: boolean;
  results_release_authorized: boolean;
  has_preferences: boolean;
  has_voted: boolean;
}
