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

export interface AdminDashboardCounts {
  students_total: number;
  users_total: number;
  proposals_pending: number;
  proposals_approved: number;
  proposals_rejected: number;
  preferences_responses: number;
  votes_cast: number;
  admins_total: number;
}

export interface AdminDashboardResponse {
  survey_open: boolean;
  proposal_submission_open: boolean;
  voting_open: boolean;
  results_release_authorized: boolean;
  counts: AdminDashboardCounts;
  progress: {
    survey_participation: number | null;
    survey_completed: number;
  };
}

export interface AdminProposalAuthor {
  id: number;
  email: string;
  full_name: string;
}

export interface AdminProposal {
  id: number;
  title: string;
  description: string;
  status: ProposalStatus;
  base_color: "BLACK" | "WHITE";
  animal: string;
  additional_colors: string[];
  has_collar: boolean;
  has_phrase: boolean;
  phrase_text: string;
  created_at: string;
  updated_at: string;
  reviewed_at: string | null;
  rejection_reason: string;
  image_url: string;
  author: AdminProposalAuthor;
  reviewed_by: AdminProposalAuthor | null;
}

export interface AdminUser {
  id: number;
  email: string;
  full_name: string;
  role: "STUDENT" | "ADMIN";
  is_active: boolean;
  is_staff: boolean;
}
