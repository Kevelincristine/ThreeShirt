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

export interface StudentPreferences {
  base_color: "BLACK" | "WHITE";
  related_to_course: boolean;
  wants_animal: boolean;
  preferred_animal: string;
  preferred_colors: string[];
  wants_collar: boolean;
  wants_phrase: boolean;
  intends_to_buy: boolean;
}

export interface StudentPreferencesResponse {
  has_preferences: boolean;
  preferences: StudentPreferences | null;
}

export interface StudentProposal {
  id: number;
  title: string;
  description: string;
  image_url: string;
  base_color: "BLACK" | "WHITE";
  related_to_course: boolean;
  animal: string;
  additional_colors: string[];
  has_collar: boolean;
  has_phrase: boolean;
  phrase_text: string;
}

export type ProposalStatus = "PENDING" | "APPROVED" | "REJECTED";

export interface StudentSubmission {
  id: number;
  title: string;
  description: string;
  status: ProposalStatus;
  created_at: string;
}

export interface StudentResultsProposal {
  proposal_id: number;
  title: string;
  vote_count: number;
  percentage: number | null;
  compatibility: number | null;
  valid_assessments: number;
}

export interface StudentResultsResponse {
  status: "complete" | "no_votes";
  total_votes: number;
  proposals: StudentResultsProposal[];
  winners: Array<Pick<StudentResultsProposal, "proposal_id" | "title" | "vote_count" | "percentage">>;
  is_tie: boolean;
  compatibility_status: "available" | "insufficient_data";
  compatibility_winners: Array<{
    proposal_id: number;
    title: string;
    valid_assessments: number;
    average_compatibility: number | null;
    status: "available" | "insufficient_data";
  }>;
  compatibility_is_tie: boolean;
}
