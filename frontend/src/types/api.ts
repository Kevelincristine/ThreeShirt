export interface ApiMessageResponse {
  message: string;
}

export type SessionRole = "student" | "student_admin" | "admin";

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
  proposal_submission_open: boolean;
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
  author_id: number;
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
  image_url: string;
  vote_count: number;
  percentage: number | null;
  compatibility: number | null;
  valid_assessments: number;
}

export interface StudentSurveyOption {
  value: string;
  label: string;
  response_count: number;
  percentage: number | null;
}

export interface StudentSurveyQuestion {
  id: string;
  question: string;
  respondent_count: number;
  options: StudentSurveyOption[];
}

export interface StudentSurveyResults {
  respondent_count: number;
  questions: StudentSurveyQuestion[];
}

export interface StudentResultsResponse {
  status: "complete" | "no_votes";
  total_votes: number;
  proposals: StudentResultsProposal[];
  winners: Array<Pick<StudentResultsProposal, "proposal_id" | "title" | "vote_count" | "percentage">>;
  is_tie: boolean;
  survey_results: StudentSurveyResults;
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

export interface AdminProposalPreferences {
  base_color: "BLACK" | "WHITE";
  related_to_course: boolean;
  wants_animal: boolean;
  preferred_animal: string;
  preferred_colors: string[];
  wants_collar: boolean;
  wants_phrase: boolean;
  intends_to_buy: boolean;
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
  preferences: AdminProposalPreferences | null;
  author: AdminProposalAuthor;
  reviewed_by: AdminProposalAuthor | null;
}

export interface AdminUser {
  id: number;
  email: string;
  full_name: string;
  role: "STUDENT" | "STUDENT_ADMIN" | "ADMIN";
  is_active: boolean;
  is_staff: boolean;
  has_password: boolean;
  has_participation: boolean;
}

export type AdminMaintenanceAction =
  | "reset_user_basic"
  | "reset_user_full"
  | "delete_user"
  | "delete_proposal"
  | "reset_election";

export type AdminMaintenanceOperation =
  | "RESET_USER_BASIC"
  | "RESET_USER_FULL"
  | "DELETE_USER"
  | "DELETE_PROPOSAL"
  | "RESET_ELECTION";

export interface AdminMaintenancePreview {
  challenge_token: string;
  expires_at: string;
  action: AdminMaintenanceOperation;
  target: {
    id?: number;
    label: string;
    email?: string;
    status?: ProposalStatus;
    author?: string;
  };
  summary: Record<string, number>;
  warning: string;
  restored_settings?: {
    survey_open: boolean;
    proposal_submission_open: boolean;
    voting_open: boolean;
    results_release_authorized: boolean;
    compatibility_weight: number;
  };
}

export interface AdminUserHistory {
  user_id: number;
  participations: Array<{
    identity_id: number;
    generation: number;
    label: string;
    current: boolean;
    eligible: boolean;
    created_at: string;
    ended_at: string | null;
    responses: number;
    votes: number;
    proposals: number;
  }>;
  participation_audit: Array<{
    id: number;
    type: string;
    participation_id: number;
    action: string;
    reason: string;
    actor_id: number;
    created_at: string;
  }>;
  account_audit: Array<{
    id: number;
    action: string;
    reason: string;
    actor_id: number;
    created_at: string;
  }>;
}

export interface AdminMaintenanceResult {
  message: string;
  affected: Record<string, number>;
  warning: string;
  file_cleanup_pending: boolean;
  backup?: {
    path: string;
    sha256: string;
    integrity: "ok";
  };
}
