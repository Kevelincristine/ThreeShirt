import { afterEach, beforeAll, beforeEach, expect, test, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { App } from "../../src/app/App";
import { AuthProvider } from "../../src/app/AuthContext";
import { RequireAdmin } from "../../src/app/RequireAdmin";
import { RequireStudent } from "../../src/app/RequireStudent";
import { AdminDashboardPage } from "../../src/pages/AdminDashboardPage";
import { HomePage } from "../../src/pages/HomePage";
import { StudentGalleryPage } from "../../src/pages/StudentGalleryPage";
import { StudentDashboardPage } from "../../src/pages/StudentDashboardPage";
import { StudentLoginPage } from "../../src/pages/StudentLoginPage";
import { StudentPreferencesPage } from "../../src/pages/StudentPreferencesPage";
import { StudentProposalsPage } from "../../src/pages/StudentProposalsPage";
import { StudentResultsPage } from "../../src/pages/StudentResultsPage";
import type { StudentResultsResponse } from "../../src/types/api";
import {
  ApiError,
  castStudentVote,
  confirmAdminMaintenance,
  changePassword,
  getApprovedStudentProposals,
  getAdminDashboard,
  getAdminProposals,
  getAdminStages,
  getAdminUsers,
  getSession,
  getStudentDashboard,
  getStudentPreferences,
  getStudentResults,
  getStudentSubmissions,
  logoutSession,
  previewAdminMaintenance,
  loginWithPassword,
  setFirstAccessPassword,
  saveStudentPreferences,
  submitStudentProposal,
  updateAdminStage,
  updateAdminUserRole,
  updateAdminUserStatus,
} from "../../src/services/api";

vi.mock("../../src/services/api", () => ({
  ApiError: class ApiError extends Error {
    constructor(message: string, public readonly status: number) {
      super(message);
    }
  },
  castStudentVote: vi.fn(),
  confirmAdminMaintenance: vi.fn(),
  changePassword: vi.fn(),
  getApprovedStudentProposals: vi.fn(),
  getAdminDashboard: vi.fn(),
  getAdminProposals: vi.fn(),
  getAdminStages: vi.fn(),
  getAdminUsers: vi.fn(),
  getSession: vi.fn(),
  getStudentDashboard: vi.fn(),
  getStudentPreferences: vi.fn(),
  getStudentResults: vi.fn(),
  getStudentSubmissions: vi.fn(),
  logoutSession: vi.fn(),
  previewAdminMaintenance: vi.fn(),
  loginWithPassword: vi.fn(),
  setFirstAccessPassword: vi.fn(),
  saveStudentPreferences: vi.fn(),
  submitStudentProposal: vi.fn(),
  updateAdminStage: vi.fn(),
  updateAdminUserRole: vi.fn(),
  updateAdminUserStatus: vi.fn(),
}));

const dashboard = {
  survey_open: true,
  proposal_submission_open: true,
  voting_open: false,
  results_release_authorized: false,
  has_preferences: false,
  has_voted: false,
};

const preferences = {
  base_color: "BLACK" as const,
  related_to_course: true,
  wants_animal: false,
  preferred_animal: "",
  preferred_colors: ["BLUE"],
  wants_collar: true,
  wants_phrase: false,
  intends_to_buy: true,
};

function makeStudentResults(
  overrides: Partial<StudentResultsResponse> = {},
): StudentResultsResponse {
  return {
    status: "complete",
    total_votes: 1,
    proposals: [],
    winners: [],
    is_tie: false,
    survey_results: { respondent_count: 0, questions: [] },
    compatibility_status: "insufficient_data",
    compatibility_winners: [],
    compatibility_is_tie: false,
    ...overrides,
  };
}

function completeSurveyForm() {
  fireEvent.change(screen.getByLabelText("Cor-base preferida"), {
    target: { value: "BLACK" },
  });
  fireEvent.change(
    screen.getByLabelText("Você gostaria que a camiseta fosse relacionada ao curso?"),
    { target: { value: "true" } },
  );
  fireEvent.change(
    screen.getByLabelText("Você gostaria de um tema com animal?"),
    { target: { value: "false" } },
  );
  fireEvent.click(screen.getByLabelText("Azul"));
  fireEvent.change(
    screen.getByLabelText("Você prefere camiseta com gola?"),
    { target: { value: "true" } },
  );
  fireEvent.change(
    screen.getByLabelText("Você gostaria de uma frase na camiseta?"),
    { target: { value: "false" } },
  );
  fireEvent.change(
    screen.getByLabelText("Você pretende comprar a camiseta?"),
    { target: { value: "true" } },
  );
}

beforeAll(() => {
  if (!HTMLDialogElement.prototype.showModal) {
    HTMLDialogElement.prototype.showModal = function showModal() {
      this.setAttribute("open", "");
    };
    HTMLDialogElement.prototype.close = function close() {
      this.removeAttribute("open");
    };
  }
});

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getStudentDashboard).mockResolvedValue(dashboard);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

test("successful password login opens the student panel", async () => {
  const user = userEvent.setup();
  vi.mocked(getSession)
    .mockResolvedValueOnce({ authenticated: false })
    .mockResolvedValueOnce({
      authenticated: true,
      user: { id: 5, email: "student@school.test", role: "student" },
    });
  vi.mocked(loginWithPassword).mockResolvedValue({
    message: "Login realizado com sucesso.",
  });

  render(
    <MemoryRouter initialEntries={["/login"]}>
      <AuthProvider>
        <Routes>
          <Route element={<StudentLoginPage />} path="/login" />
          <Route element={<p>Painel depois do login</p>} path="/student" />
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  );

  await user.type(
    await screen.findByLabelText("Gmail autorizado"),
    "student@school.test",
  );
  await user.type(screen.getByLabelText("Senha"), "TestPassword!123");
  await user.click(screen.getByRole("button", { name: "Entrar" }));

  expect(await screen.findByText("Painel depois do login")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Primeiro acesso" })).toBeNull();
  expect(loginWithPassword).toHaveBeenCalledWith(
    "student@school.test",
    "TestPassword!123",
  );
});

test("first access sets a password for an existing account without approval", async () => {
  const user = userEvent.setup();
  vi.mocked(setFirstAccessPassword).mockResolvedValue({
    message: "Senha definida. Entre com seu e-mail e a nova senha.",
  });

  render(
    <MemoryRouter initialEntries={["/login"]}>
      <AuthProvider>
        <Routes>
          <Route element={<StudentLoginPage />} path="/login" />
          <Route
            element={<StudentLoginPage firstAccess key="first-access" />}
            path="/first-access"
          />
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  );

  await user.click(
    await screen.findByRole("link", {
      name: "Primeiro acesso? Defina sua senha",
    }),
  );
  expect(await screen.findByRole("heading", { name: "Primeiro acesso" }))
    .toBeTruthy();

  await user.type(
    screen.getByLabelText("Gmail autorizado"),
    "student@school.test",
  );
  await user.type(screen.getByLabelText("Nova senha"), "TestPassword!123");
  await user.type(screen.getByLabelText("Confirmar nova senha"), "TestPassword!123");
  await user.click(screen.getByRole("button", { name: "Definir senha" }));

  expect(await screen.findByText(
    "Senha definida. Entre com seu e-mail e a nova senha.",
  )).toBeTruthy();
  expect(setFirstAccessPassword).toHaveBeenCalledWith(
    "student@school.test",
    "TestPassword!123",
    "TestPassword!123",
  );
  expect(screen.getByRole("button", { name: "Entrar" })).toBeTruthy();
  expect(screen.queryByText(/aprovação|aguardando/i)).toBeNull();
});

test.each([
  ["admin", "/admin", "Painel administrativo"],
  ["student_admin", "/student", "Painel estudante-administrador"],
] as const)(
  "successful %s login redirects to its role-appropriate panel",
  async (role, destination, panel) => {
    const user = userEvent.setup();
    vi.mocked(getSession)
      .mockResolvedValueOnce({ authenticated: false })
      .mockResolvedValueOnce({
        authenticated: true,
        user: { id: 8, email: `${role}@school.test`, role },
      });
    vi.mocked(loginWithPassword).mockResolvedValue({
      message: "Login realizado com sucesso.",
    });

    render(
      <MemoryRouter initialEntries={["/login"]}>
        <AuthProvider>
          <Routes>
            <Route element={<StudentLoginPage />} path="/login" />
            <Route element={<p>{panel}</p>} path={destination} />
          </Routes>
        </AuthProvider>
      </MemoryRouter>,
    );

    await user.type(
      await screen.findByLabelText("Gmail autorizado"),
      `${role}@school.test`,
    );
    await user.type(screen.getByLabelText("Senha"), "TestPassword!123");
    await user.click(screen.getByRole("button", { name: "Entrar" }));

    expect(await screen.findByText(panel)).toBeTruthy();
  },
);

test("logout asks for confirmation and cancel keeps the session active", async () => {
  const user = userEvent.setup();
  vi.mocked(getSession).mockResolvedValue({
    authenticated: true,
    user: { id: 5, email: "student@school.test", role: "student" },
  });
  vi.mocked(logoutSession).mockResolvedValue({
    authenticated: false,
    message: "Sessão encerrada.",
  });

  render(
    <MemoryRouter initialEntries={["/student"]}>
      <AuthProvider>
        <Routes>
          <Route element={<App />}>
            <Route element={<p>Painel ativo</p>} path="student" />
            <Route element={<p>Tela de login</p>} path="login" />
          </Route>
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  );

  await user.click(await screen.findByRole("button", { name: "Sair" }));
  const dialog = screen.getByRole("dialog", { name: "Confirmar saída" });
  await user.click(
    within(dialog).getByRole("button", { name: "Continuar conectado" }),
  );
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(logoutSession).not.toHaveBeenCalled();
  expect(screen.getByText("Painel ativo")).toBeTruthy();

  await user.click(screen.getByRole("button", { name: "Sair" }));
  const confirmDialog = screen.getByRole("dialog", {
    name: "Confirmar saída",
  });
  await user.click(within(confirmDialog).getByRole("button", { name: "Sair" }));

  expect(await screen.findByText("Tela de login")).toBeTruthy();
  expect(logoutSession).toHaveBeenCalledTimes(1);
});

test("an admin can navigate between election and admin areas in one session", async () => {
  const user = userEvent.setup();
  vi.mocked(getSession).mockResolvedValue({
    authenticated: true,
    user: { id: 8, email: "admin@school.test", role: "admin" },
  });

  render(
    <MemoryRouter initialEntries={["/"]}>
      <AuthProvider>
        <Routes>
          <Route element={<App />}>
            <Route element={<HomePage />} index />
            <Route
              element={<p>Painel da eleição protegido</p>}
              path="student"
            />
            <Route
              element={
                <RequireAdmin>
                  <p>Painel administrativo protegido</p>
                </RequireAdmin>
              }
              path="admin"
            />
          </Route>
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  );

  await screen.findByRole("link", { name: "Painel da eleição" });
  await user.click(screen.getByRole("link", { name: "Painel da eleição" }));
  expect(await screen.findByText("Painel da eleição protegido")).toBeTruthy();
  await user.click(screen.getByRole("link", { name: "Administração" }));
  expect(await screen.findByText("Painel administrativo protegido")).toBeTruthy();
  expect(getSession).toHaveBeenCalledTimes(1);
});

test("a student administrator sees both menus and enters the research route", async () => {
  const user = userEvent.setup();
  vi.mocked(getSession).mockResolvedValue({
    authenticated: true,
    user: { id: 9, email: "hybrid@school.test", role: "student_admin" },
  });

  render(
    <MemoryRouter initialEntries={["/"]}>
      <AuthProvider>
        <Routes>
          <Route element={<App />}>
            <Route element={<HomePage />} index />
            <Route
              element={
                <RequireStudent>
                  <p>Painel estudantil protegido</p>
                </RequireStudent>
              }
              path="student"
            />
            <Route
              element={
                <RequireStudent>
                  <p>Preferências protegidas</p>
                </RequireStudent>
              }
              path="student/survey"
            />
            <Route
              element={
                <RequireAdmin>
                  <p>Administração protegida</p>
                </RequireAdmin>
              }
              path="admin"
            />
          </Route>
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  );

  expect(await screen.findByText("Painel estudantil protegido")).toBeTruthy();
  expect(screen.getByRole("link", { name: "Propostas" })).toBeTruthy();
  expect(screen.getByRole("link", { name: "Administração" })).toBeTruthy();
  expect(screen.queryByRole("link", { name: "Início" })).toBeNull();
  await user.click(screen.getByRole("link", { name: "Pesquisa" }));
  expect(await screen.findByText("Preferências protegidas")).toBeTruthy();
  await user.click(screen.getByRole("link", { name: "Administração" }));
  expect(await screen.findByText("Administração protegida")).toBeTruthy();
  expect(getSession).toHaveBeenCalledTimes(1);
});

test("a student administrator keeps student actions on both student panels", async () => {
  vi.mocked(getSession).mockResolvedValue({
    authenticated: true,
    user: { id: 9, email: "hybrid@school.test", role: "student_admin" },
  });
  vi.mocked(getStudentDashboard).mockResolvedValue({
    ...dashboard,
    voting_open: true,
  });
  vi.mocked(getApprovedStudentProposals).mockResolvedValue([{
    id: 3,
    author_id: 12,
    title: "Proposta aprovada",
    description: "Design aprovado",
    image_url: "/proposal/3/image/",
    base_color: "BLACK",
    related_to_course: true,
    animal: "",
    additional_colors: ["BLUE"],
    has_collar: true,
    has_phrase: false,
    phrase_text: "",
  }]);

  render(
    <MemoryRouter>
      <AuthProvider>
        <StudentDashboardPage />
      </AuthProvider>
    </MemoryRouter>,
  );
  expect(await screen.findByRole("link", { name: "Responder pesquisa" })).toBeTruthy();
  expect(screen.getByLabelText("Senha atual")).toBeTruthy();
  expect(screen.getByText("Sem voto registrado")).toBeTruthy();

  cleanup();
  render(
    <MemoryRouter>
      <AuthProvider>
        <StudentGalleryPage />
      </AuthProvider>
    </MemoryRouter>,
  );
  expect(await screen.findByRole("button", { name: "Votar nesta proposta" })).toBeTruthy();
});

test("admin user management offers role controls without first-access credentials", async () => {
  const user = userEvent.setup();
  const prompt = vi.spyOn(window, "prompt").mockReturnValue("Validação administrativa");
  vi.mocked(getSession).mockResolvedValue({
    authenticated: true,
    user: { id: 8, email: "admin@school.test", role: "admin" },
  });
  vi.mocked(getAdminDashboard).mockResolvedValue({
    survey_open: false,
    proposal_submission_open: false,
    voting_open: false,
    results_release_authorized: false,
    counts: {
      students_total: 1,
      users_total: 1,
      proposals_pending: 0,
      proposals_approved: 0,
      proposals_rejected: 0,
      preferences_responses: 0,
      votes_cast: 0,
      admins_total: 0,
    },
    progress: { survey_participation: 0, survey_completed: 0 },
  });
  vi.mocked(getAdminProposals).mockResolvedValue([]);
  vi.mocked(getAdminStages).mockResolvedValue({
    survey_open: false,
    proposal_submission_open: false,
    voting_open: false,
    results_release_authorized: false,
  });
  vi.mocked(getAdminUsers).mockResolvedValue([
    {
      id: 44,
      email: "student@school.test",
      full_name: "Estudante",
      role: "STUDENT",
      is_active: true,
      is_staff: false,
      has_password: true,
      has_participation: false,
    },
    {
      id: 45,
      email: "hybrid@school.test",
      full_name: "Estudante Admin",
      role: "STUDENT_ADMIN",
      is_active: false,
      is_staff: false,
      has_password: false,
      has_participation: true,
    },
    {
      id: 46,
      email: "new-student@school.test",
      full_name: "Novo estudante",
      role: "STUDENT",
      is_active: true,
      is_staff: false,
      has_password: false,
      has_participation: false,
    },
  ]);
  vi.mocked(updateAdminUserRole).mockResolvedValue({ message: "Atualizado." });
  vi.mocked(changePassword).mockResolvedValue({
    message: "Senha alterada com sucesso.",
  });

  render(
    <MemoryRouter>
      <AuthProvider>
        <AdminDashboardPage />
      </AuthProvider>
    </MemoryRouter>,
  );

  expect(await screen.findAllByText("Conta ativa")).toHaveLength(2);
  expect(screen.getByRole("heading", { name: "Zona de Manutenção" })).toBeTruthy();
  expect(screen.getByText("Conta inativa")).toBeTruthy();
  expect(screen.getByText("Estudante e administrador")).toBeTruthy();
  await user.type(screen.getByLabelText("Senha atual"), "Initial-secret");
  await user.type(screen.getByLabelText("Nova senha"), "Replacement-secret-78!");
  await user.type(
    screen.getByLabelText("Confirmar nova senha"),
    "Replacement-secret-78!",
  );
  await user.click(screen.getByRole("button", { name: "Alterar senha" }));
  await waitFor(() => {
    expect(changePassword).toHaveBeenCalledWith(
      "Initial-secret",
      "Replacement-secret-78!",
      "Replacement-secret-78!",
    );
  });

  const studentUser = screen.getByText("student@school.test").closest("article")!;
  await user.click(within(studentUser).getByRole("button", {
    name: "Definir: Estudante e administrador",
  }));
  await waitFor(() => {
    expect(updateAdminUserRole).toHaveBeenCalledWith(
      44,
      "STUDENT_ADMIN",
      "Validação administrativa",
    );
  });
  expect(
    screen.queryByRole("button", {
      name: "Emitir credencial de primeiro acesso",
    }),
  ).toBeNull();
  prompt.mockRestore();
});

test("a student administrator can see the admin dashboard but not maintenance controls", async () => {
  vi.mocked(getSession).mockResolvedValue({
    authenticated: true,
    user: { id: 9, email: "hybrid@school.test", role: "student_admin" },
  });
  vi.mocked(getAdminDashboard).mockResolvedValue({
    survey_open: false,
    proposal_submission_open: true,
    voting_open: false,
    results_release_authorized: false,
    counts: {
      students_total: 1,
      users_total: 1,
      proposals_pending: 0,
      proposals_approved: 0,
      proposals_rejected: 0,
      preferences_responses: 0,
      votes_cast: 0,
      admins_total: 1,
    },
    progress: { survey_participation: 0, survey_completed: 0 },
  });
  vi.mocked(getAdminProposals).mockResolvedValue([]);
  vi.mocked(getAdminStages).mockResolvedValue({
    survey_open: false,
    proposal_submission_open: true,
    voting_open: false,
    results_release_authorized: false,
  });
  vi.mocked(getAdminUsers).mockResolvedValue([]);

  render(
    <MemoryRouter>
      <AuthProvider>
        <AdminDashboardPage />
      </AuthProvider>
    </MemoryRouter>,
  );

  expect(await screen.findByRole("heading", { name: "Painel administrativo" })).toBeTruthy();
  expect(screen.getByRole("heading", { name: "Zona de Manutenção" })).toBeTruthy();
  expect(
    screen.queryByRole("button", { name: /reinicialização eleitoral/i }),
  ).toBeNull();
});

test("admin account reset requires a justified preview before confirmation", async () => {
  const user = userEvent.setup();
  vi.spyOn(window, "confirm").mockReturnValue(true);
  vi.spyOn(window, "prompt").mockReturnValue("Conta transferida mediante registro");
  vi.mocked(getSession).mockResolvedValue({
    authenticated: true,
    user: { id: 8, email: "admin@school.test", role: "admin" },
  });
  vi.mocked(getAdminDashboard).mockResolvedValue({
    survey_open: false,
    proposal_submission_open: true,
    voting_open: false,
    results_release_authorized: false,
    counts: {
      students_total: 1,
      users_total: 2,
      proposals_pending: 1,
      proposals_approved: 0,
      proposals_rejected: 0,
      preferences_responses: 0,
      votes_cast: 0,
      admins_total: 1,
    },
    progress: { survey_participation: 0, survey_completed: 0 },
  });
  vi.mocked(getAdminProposals).mockResolvedValue([]);
  vi.mocked(getAdminStages).mockResolvedValue({
    survey_open: false,
    proposal_submission_open: true,
    voting_open: false,
    results_release_authorized: false,
  });
  vi.mocked(getAdminUsers).mockResolvedValue([
    {
      id: 44,
      email: "student@school.test",
      full_name: "Estudante",
      role: "STUDENT",
      is_active: true,
      is_staff: false,
      has_password: true,
      has_participation: true,
    },
  ]);
  vi.mocked(previewAdminMaintenance).mockResolvedValue({
    challenge_token: "one-time-token",
    expires_at: "2026-10-31T12:00:00-03:00",
    action: "RESET_USER_BASIC",
    target: {
      id: 44,
      label: "Estudante",
      email: "student@school.test",
    },
    summary: {
      account_preserved: 1,
      email_preserved: 1,
      profiles_reset: 1,
      responses_preserved: 1,
      votes_preserved: 2,
      proposals_preserved: 1,
    },
    warning: "",
  });
  vi.mocked(confirmAdminMaintenance).mockResolvedValue({
    message: "Conta reinicializada; o histórico eleitoral foi preservado.",
    affected: { account_preserved: 1, responses_preserved: 1, votes_preserved: 2 },
    warning: "",
    file_cleanup_pending: false,
  });

  render(
    <MemoryRouter>
      <AuthProvider>
        <AdminDashboardPage />
      </AuthProvider>
    </MemoryRouter>,
  );

  await user.selectOptions(
    await screen.findByLabelText("Conta"),
    "44",
  );
  await user.type(
    screen.getByLabelText("Justificativa obrigatória"),
    "Conta transferida mediante registro",
  );
  await user.click(
    screen.getByRole("button", {
      name: "Revisar reinicialização preservando histórico",
    }),
  );
  await waitFor(() => {
    expect(previewAdminMaintenance).toHaveBeenCalledWith(
      "reset_user_basic",
      44,
      "Conta transferida mediante registro",
    );
  });
  expect(await screen.findByText("Votos históricos preservados")).toBeTruthy();
  await user.click(
    screen.getByLabelText("Li a prévia e autorizo explicitamente esta operação."),
  );
  await user.click(screen.getByRole("button", { name: "Confirmar definitivamente" }));
  await waitFor(() => {
    expect(confirmAdminMaintenance).toHaveBeenCalledWith("one-time-token", "");
  });
});

test("an admin is restricted from student participation routes", async () => {
  vi.mocked(getSession).mockResolvedValue({
    authenticated: true,
    user: { id: 8, email: "admin@school.test", role: "admin" },
  });

  render(
    <MemoryRouter initialEntries={["/student/preferences"]}>
      <AuthProvider>
        <Routes>
          <Route
            element={
              <RequireStudent>
                <p>Formulário de preferências</p>
              </RequireStudent>
            }
            path="/student/preferences"
          />
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  );

  expect(
    await screen.findByText("Esta funcionalidade é exclusiva para estudantes elegíveis."),
  ).toBeTruthy();
  expect(screen.queryByText("Formulário de preferências")).toBeNull();
});

test("an admin dashboard shows general election status without participant actions", async () => {
  vi.mocked(getSession).mockResolvedValue({
    authenticated: true,
    user: { id: 8, email: "admin@school.test", role: "admin" },
  });
  vi.mocked(getStudentDashboard).mockResolvedValue({
    ...dashboard,
    survey_open: true,
    voting_open: true,
  });

  render(
    <MemoryRouter>
      <AuthProvider>
        <StudentDashboardPage />
      </AuthProvider>
    </MemoryRouter>,
  );

  expect(await screen.findByText("Pesquisa")).toBeTruthy();
  expect(screen.queryByText("Suas preferências")).toBeNull();
  expect(screen.queryByText("Seu voto")).toBeNull();
  expect(screen.getByRole("link", { name: "Ver propostas aprovadas" })).toBeTruthy();
  expect(screen.queryByRole("link", { name: "Responder pesquisa" })).toBeNull();
});

test("a student cannot enter the admin area", async () => {
  vi.mocked(getSession).mockResolvedValue({
    authenticated: true,
    user: { id: 5, email: "student@school.test", role: "student" },
  });

  render(
    <MemoryRouter initialEntries={["/admin"]}>
      <AuthProvider>
        <Routes>
          <Route
            element={
              <RequireAdmin>
                <p>Painel administrativo protegido</p>
              </RequireAdmin>
            }
            path="/admin"
          />
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  );

  expect(await screen.findByText("Área exclusiva para administradores")).toBeTruthy();
  expect(screen.queryByText("Painel administrativo protegido")).toBeNull();
});

test("an unauthenticated user is redirected away from the student area", async () => {
  vi.mocked(getSession).mockResolvedValue({ authenticated: false });

  render(
    <MemoryRouter initialEntries={["/student"]}>
      <AuthProvider>
        <Routes>
          <Route
            element={
              <RequireStudent>
                <p>Painel protegido</p>
              </RequireStudent>
            }
            path="/student"
          />
          <Route element={<p>Entre novamente</p>} path="/login" />
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  );

  expect(await screen.findByText("Entre novamente")).toBeTruthy();
  expect(screen.queryByText("Painel protegido")).toBeNull();
});

test("preferences show a loading state while the backend response is pending", () => {
  vi.mocked(getStudentDashboard).mockReturnValue(new Promise(() => {}));
  vi.mocked(getStudentPreferences).mockReturnValue(new Promise(() => {}));

  render(<StudentPreferencesPage />);

  expect(screen.getByText("Carregando pesquisa...")).toBeTruthy();
});

test("the survey starts incomplete and only becomes ready after required answers", async () => {
  vi.mocked(getStudentPreferences).mockResolvedValue({
    has_preferences: false,
    preferences: null,
  });

  render(<StudentPreferencesPage />);
  await screen.findByRole("heading", { name: "Pesquisa" });
  expect(screen.getByText("Pesquisa incompleta")).toBeTruthy();
  expect(
    screen.getByRole("button", { name: "Responda às perguntas obrigatórias" })
      .hasAttribute("disabled"),
  ).toBe(true);

  completeSurveyForm();
  expect(screen.getByText("Respostas prontas para envio")).toBeTruthy();
  expect(
    screen.getByRole("button", { name: "Enviar respostas" })
      .hasAttribute("disabled"),
  ).toBe(false);
  expect(saveStudentPreferences).not.toHaveBeenCalled();
});

test("the survey requires an animal name when that option is selected", async () => {
  vi.mocked(getStudentPreferences).mockResolvedValue({
    has_preferences: false,
    preferences: null,
  });

  render(<StudentPreferencesPage />);
  await screen.findByRole("heading", { name: "Pesquisa" });
  completeSurveyForm();
  fireEvent.change(
    screen.getByLabelText("Você gostaria de um tema com animal?"),
    { target: { value: "true" } },
  );

  expect(screen.getByText("Pesquisa incompleta")).toBeTruthy();
  expect(
    screen.getByRole("button", { name: "Responda às perguntas obrigatórias" })
      .hasAttribute("disabled"),
  ).toBe(true);

  fireEvent.change(screen.getByLabelText("Qual animal?"), {
    target: { value: "Gato" },
  });
  expect(screen.getByText("Respostas prontas para envio")).toBeTruthy();
});

test("the survey saves complete answers and shows backend-confirmed success", async () => {
  vi.mocked(getStudentDashboard).mockResolvedValue(dashboard);
  vi.mocked(getStudentPreferences).mockResolvedValue({
    has_preferences: false,
    preferences: null,
  });
  vi.mocked(saveStudentPreferences).mockResolvedValue({
    message: "Preferências salvas com sucesso.",
    has_preferences: true,
    preferences,
  });

  render(<StudentPreferencesPage />);
  await screen.findByRole("heading", { name: "Pesquisa" });
  completeSurveyForm();
  fireEvent.submit(
    screen.getByRole("button", { name: "Enviar respostas" }).closest("form")!,
  );

  expect(saveStudentPreferences).toHaveBeenCalledWith(preferences);
  expect(await screen.findByText("Preferências salvas com sucesso.")).toBeTruthy();
  expect(screen.getByText("Pesquisa já enviada")).toBeTruthy();
});

test("the survey displays save failures beside the submit action", async () => {
  vi.mocked(getStudentPreferences).mockResolvedValue({
    has_preferences: false,
    preferences: null,
  });
  vi.mocked(saveStudentPreferences).mockRejectedValue(
    new ApiError("Falha ao salvar respostas.", 500),
  );

  render(<StudentPreferencesPage />);
  await screen.findByRole("heading", { name: "Pesquisa" });
  completeSurveyForm();
  fireEvent.submit(
    screen.getByRole("button", { name: "Enviar respostas" }).closest("form")!,
  );

  const error = await screen.findByRole("alert");
  expect(error.textContent).toBe("Falha ao salvar respostas.");
  expect(
    error.compareDocumentPosition(screen.getByRole("button", {
      name: "Enviar respostas",
    })) & Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();
});

test("closed preference survey displays saved answers and prevents submission", async () => {
  vi.mocked(getStudentDashboard).mockResolvedValue({
    ...dashboard,
    survey_open: false,
    has_preferences: true,
  });
  vi.mocked(getStudentPreferences).mockResolvedValue({
    has_preferences: true,
    preferences,
  });

  render(<StudentPreferencesPage />);

  expect(await screen.findByText("Pesquisa fechada", { selector: "strong" })).toBeTruthy();
  expect(screen.getByText(/Não é possível enviar ou atualizar respostas/)).toBeTruthy();
  expect(screen.getByRole("button", { name: "Pesquisa fechada" }).hasAttribute("disabled")).toBe(true);
  expect(
    screen.getByLabelText("Cor-base preferida").matches(":disabled"),
  ).toBe(true);
  expect(saveStudentPreferences).not.toHaveBeenCalled();
});

test("proposal form sends a multipart proposal and handles an empty history", async () => {
  const user = userEvent.setup();
  vi.mocked(getStudentSubmissions).mockResolvedValue([]);
  vi.mocked(submitStudentProposal).mockResolvedValue({
    id: 9,
    title: "Camiseta da turma",
    description: "Uma ideia",
    status: "PENDING",
  });

  render(<StudentProposalsPage />);
  await screen.findByText("Você ainda não enviou propostas.");
  expect(await screen.findByRole("button", { name: "Enviar para análise" })).toBeTruthy();
  fireEvent.change(screen.getByLabelText("Título"), {
    target: { value: "Camiseta da turma" },
  });
  fireEvent.change(screen.getByLabelText("Descrição"), {
    target: { value: "Uma ideia" },
  });
  const file = new File(["shirt image"], "design.png", { type: "image/png" });
  const imageInput = screen.getByLabelText(/Imagem do design/) as HTMLInputElement;
  await user.upload(imageInput, file);
  expect(imageInput.files?.[0]).toBe(file);
  expect(screen.getByLabelText("Título")).toHaveProperty("value", "Camiseta da turma");
  expect(screen.getByLabelText("Descrição")).toHaveProperty("value", "Uma ideia");
  fireEvent.submit(imageInput.closest("form")!);

  await waitFor(() => expect(submitStudentProposal).toHaveBeenCalledTimes(1));
  const payload = vi.mocked(submitStudentProposal).mock.calls[0][0];
  expect(payload.get("title")).toBe("Camiseta da turma");
  expect(payload.get("related_to_course")).toBe("false");
  expect(payload.get("additional_colors")).toBe("[]");
  expect(payload.get("image")).toBe(file);
  expect(await screen.findByText(/foi enviada e aguarda análise/)).toBeTruthy();
});

test("closed proposal submission hides the form and explains the stage", async () => {
  vi.mocked(getStudentDashboard).mockResolvedValue({
    ...dashboard,
    proposal_submission_open: false,
  });
  vi.mocked(getStudentSubmissions).mockResolvedValue([]);

  const { container } = render(<StudentProposalsPage />);

  expect(await screen.findByText(/O envio de propostas está fechado/)).toBeTruthy();
  expect(container.querySelector("form")?.hidden).toBe(true);
  expect(screen.getByText("Você ainda não enviou propostas.")).toBeTruthy();
});

test("proposal form stays hidden when stage status fails to load and can be retried", async () => {
  vi.mocked(getStudentDashboard)
    .mockRejectedValueOnce(new Error("Falha ao consultar as etapas."))
    .mockResolvedValueOnce(dashboard);
  vi.mocked(getStudentSubmissions).mockResolvedValue([]);

  const { container } = render(<StudentProposalsPage />);

  expect(await screen.findByText("Falha ao consultar as etapas.")).toBeTruthy();
  expect(container.querySelector("form")?.hidden).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Tentar verificar novamente" }));

  expect(await screen.findByRole("button", { name: "Enviar para análise" })).toBeTruthy();
  expect(container.querySelector("form")?.hidden).toBe(false);
});

test("proposal history shows each real moderation status and API errors", async () => {
  vi.mocked(getStudentSubmissions).mockResolvedValue([{
    id: 2,
    title: "Minha proposta",
    description: "Descrição própria.",
    status: "REJECTED",
    created_at: "2026-10-20T10:00:00-03:00",
  }]);

  render(<StudentProposalsPage />);
  expect(await screen.findByText("Rejeitada")).toBeTruthy();

  cleanup();
  vi.mocked(getStudentSubmissions).mockRejectedValue(new Error("Sessão expirada."));
  render(<StudentProposalsPage />);
  expect(await screen.findByText("Sessão expirada.")).toBeTruthy();
});

test("gallery displays approved proposals and asks for confirmation before voting", async () => {
  const user = userEvent.setup();
  vi.mocked(getSession).mockResolvedValue({
    authenticated: true,
    user: { id: 5, email: "student@school.test", role: "student" },
  });
  vi.mocked(getApprovedStudentProposals).mockResolvedValue([{
    id: 4,
    author_id: 12,
    title: "Design aprovado",
    description: "Aprovado pela equipe.",
    image_url: "/proposals/4/image/",
    base_color: "WHITE",
    related_to_course: true,
    animal: "",
    additional_colors: ["BLUE"],
    has_collar: true,
    has_phrase: false,
    phrase_text: "",
  }]);
  vi.mocked(getStudentDashboard)
    .mockResolvedValueOnce({ ...dashboard, voting_open: true })
    .mockResolvedValueOnce({ ...dashboard, voting_open: true, has_voted: true });
  vi.mocked(castStudentVote).mockResolvedValue();

  render(
    <AuthProvider>
      <StudentGalleryPage />
    </AuthProvider>,
  );
  await user.click(await screen.findByRole("button", { name: "Votar nesta proposta" }));
  expect(await screen.findByRole("heading", { name: "Confirmar seu voto" })).toBeTruthy();
  expect(castStudentVote).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "Confirmar voto" }));
  await waitFor(() => expect(castStudentVote).toHaveBeenCalledWith(4));
  expect(await screen.findByText("Seu voto foi confirmado pelo sistema.")).toBeTruthy();
});

test("student gallery keeps own proposals visible but disables only their vote button", async () => {
  vi.mocked(getSession).mockResolvedValue({
    authenticated: true,
    user: { id: 5, email: "student@school.test", role: "student" },
  });
  vi.mocked(getApprovedStudentProposals).mockResolvedValue([{
    id: 4,
    author_id: 5,
    title: "Minha proposta",
    description: "Design próprio.",
    image_url: "/proposals/4/image/",
    base_color: "WHITE",
    related_to_course: true,
    animal: "",
    additional_colors: [],
    has_collar: false,
    has_phrase: false,
    phrase_text: "",
  }, {
    id: 6,
    author_id: 7,
    title: "Proposta de outra pessoa",
    description: "Design de outra estudante.",
    image_url: "/proposals/6/image/",
    base_color: "BLACK",
    related_to_course: false,
    animal: "",
    additional_colors: [],
    has_collar: false,
    has_phrase: false,
    phrase_text: "",
  }]);
  vi.mocked(getStudentDashboard).mockResolvedValue({
    ...dashboard,
    voting_open: true,
    has_voted: false,
  });

  render(
    <AuthProvider>
      <StudentGalleryPage />
    </AuthProvider>,
  );

  expect(await screen.findByText("Minha proposta")).toBeTruthy();
  expect(screen.getByText("Proposta de outra pessoa")).toBeTruthy();
  const voteButtons = screen.getAllByRole("button", {
    name: "Votar nesta proposta",
  });
  expect(voteButtons[0].disabled).toBe(true);
  expect(voteButtons[1].disabled).toBe(false);
  expect(screen.getByText("Você não pode votar na sua própria proposta")).toBeTruthy();
});

test("gallery explains when no approved proposals are available", async () => {
  vi.mocked(getSession).mockResolvedValue({
    authenticated: true,
    user: { id: 5, email: "student@school.test", role: "student" },
  });
  vi.mocked(getApprovedStudentProposals).mockResolvedValue([]);
  vi.mocked(getStudentDashboard).mockResolvedValue(dashboard);

  render(
    <AuthProvider>
      <StudentGalleryPage />
    </AuthProvider>,
  );

  expect(await screen.findByText("Ainda não há propostas aprovadas para exibir.")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Votar nesta proposta" })).toBeNull();
});

test("student-admin gallery shows own proposals disabled and can vote for another author", async () => {
  const user = userEvent.setup();
  vi.mocked(getSession).mockResolvedValue({
    authenticated: true,
    user: { id: 9, email: "hybrid@school.test", role: "student_admin" },
  });
  vi.mocked(getApprovedStudentProposals).mockResolvedValue([{
    id: 14,
    author_id: 9,
    title: "Proposta própria",
    description: "Design criado pela estudante-admin.",
    image_url: "/proposals/14/image/",
    base_color: "WHITE",
    related_to_course: true,
    animal: "",
    additional_colors: ["BLUE"],
    has_collar: true,
    has_phrase: false,
    phrase_text: "",
  }, {
    id: 15,
    author_id: 10,
    title: "Proposta de outro participante",
    description: "Design aprovado.",
    image_url: "/proposals/14/image/",
    base_color: "WHITE",
    related_to_course: true,
    animal: "",
    additional_colors: ["BLUE"],
    has_collar: true,
    has_phrase: false,
    phrase_text: "",
  }]);
  vi.mocked(getStudentDashboard).mockResolvedValue({
    ...dashboard,
    voting_open: true,
    has_voted: false,
  });
  vi.mocked(castStudentVote).mockResolvedValue();

  render(
    <AuthProvider>
      <StudentGalleryPage />
    </AuthProvider>,
  );

  const buttons = await screen.findAllByRole("button", {
    name: "Votar nesta proposta",
  });
  expect(buttons[0].disabled).toBe(true);
  expect(screen.getByText("Você não pode votar na sua própria proposta")).toBeTruthy();
  await user.click(buttons[1]);
  await user.click(screen.getByRole("button", { name: "Confirmar voto" }));
  await waitFor(() => {
    expect(castStudentVote).toHaveBeenCalledWith(15);
  });
});

test("admin gallery displays approved designs without offering a vote", async () => {
  vi.mocked(getSession).mockResolvedValue({
    authenticated: true,
    user: { id: 8, email: "admin@school.test", role: "admin" },
  });
  vi.mocked(getApprovedStudentProposals).mockResolvedValue([{
    id: 4,
    author_id: 12,
    title: "Design aprovado",
    description: "Aprovado pela equipe.",
    image_url: "/proposals/4/image/",
    base_color: "WHITE",
    related_to_course: true,
    animal: "",
    additional_colors: ["BLUE"],
    has_collar: true,
    has_phrase: false,
    phrase_text: "",
  }]);
  vi.mocked(getStudentDashboard).mockResolvedValue({
    ...dashboard,
    voting_open: true,
  });

  render(
    <AuthProvider>
      <StudentGalleryPage />
    </AuthProvider>,
  );

  expect(await screen.findByText("Design aprovado")).toBeTruthy();
  expect(
    screen.getByText(/a participação na votação é restrita a estudantes elegíveis/i),
  ).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Votar nesta proposta" })).toBeNull();
});

test("administrative proposal review shows each author's read-only survey answers", async () => {
  vi.mocked(getSession).mockResolvedValue({
    authenticated: true,
    user: { id: 8, email: "admin@school.test", role: "admin" },
  });
  vi.mocked(getAdminDashboard).mockResolvedValue({
    survey_open: false,
    proposal_submission_open: false,
    voting_open: false,
    results_release_authorized: false,
    counts: {
      students_total: 2,
      users_total: 3,
      proposals_pending: 3,
      proposals_approved: 0,
      proposals_rejected: 0,
      preferences_responses: 2,
      votes_cast: 0,
      admins_total: 1,
    },
    progress: { survey_participation: 100, survey_completed: 2 },
  });
  const adminProposal = (
    id: number,
    fullName: string,
    preferences: {
      base_color: "BLACK" | "WHITE";
      related_to_course: boolean;
      wants_animal: boolean;
      preferred_animal: string;
      preferred_colors: string[];
      wants_collar: boolean;
      wants_phrase: boolean;
      intends_to_buy: boolean;
    } | null,
  ) => ({
    id,
    title: `Proposta ${fullName}`,
    description: "Design submetido para revisão.",
    status: "PENDING" as const,
    base_color: "BLACK" as const,
    animal: "",
    additional_colors: [],
    has_collar: false,
    has_phrase: false,
    phrase_text: "",
    created_at: "2026-11-02T10:00:00-03:00",
    updated_at: "2026-11-02T10:00:00-03:00",
    reviewed_at: null,
    rejection_reason: "",
    image_url: `/proposals/${id}/image/`,
    preferences,
    author: { id: id + 20, email: `${fullName}@school.test`, full_name: fullName },
    reviewed_by: null,
  });
  vi.mocked(getAdminProposals).mockResolvedValue([
    adminProposal(1, "Autora Um", {
      base_color: "BLACK",
      related_to_course: true,
      wants_animal: true,
      preferred_animal: "Lontra",
      preferred_colors: ["BLUE", "RED"],
      wants_collar: false,
      wants_phrase: true,
      intends_to_buy: true,
    }),
    adminProposal(2, "Autora Dois", {
      base_color: "WHITE",
      related_to_course: false,
      wants_animal: false,
      preferred_animal: "",
      preferred_colors: ["GREEN"],
      wants_collar: true,
      wants_phrase: false,
      intends_to_buy: false,
    }),
    adminProposal(3, "Autora Sem Pesquisa", null),
  ]);
  vi.mocked(getAdminStages).mockResolvedValue({
    survey_open: false,
    proposal_submission_open: false,
    voting_open: false,
    results_release_authorized: false,
  });
  vi.mocked(getAdminUsers).mockResolvedValue([]);

  render(
    <MemoryRouter>
      <AuthProvider>
        <AdminDashboardPage />
      </AuthProvider>
    </MemoryRouter>,
  );

  const firstAnswers = await screen.findByRole("region", {
    name: "Respostas da pesquisa de Autora Um",
  });
  const secondAnswers = screen.getByRole("region", {
    name: "Respostas da pesquisa de Autora Dois",
  });
  expect(within(firstAnswers).getByText("Lontra")).toBeTruthy();
  expect(within(firstAnswers).getByText("Azul, Vermelho")).toBeTruthy();
  expect(within(firstAnswers).getByText("O formulário registra apenas se deseja uma frase: Sim.")).toBeTruthy();
  expect(within(secondAnswers).getByText("Não se aplica")).toBeTruthy();
  expect(within(secondAnswers).getByText("Verde")).toBeTruthy();
  expect(within(secondAnswers).getByText("Intenção de compra").parentElement?.textContent).toContain("Não");
  expect(firstAnswers.querySelectorAll("input, select, textarea")).toHaveLength(0);
  expect(await screen.findByText(
    "Este estudante ainda não preencheu a pesquisa de preferências.",
  )).toBeTruthy();
  expect(screen.getAllByRole("button", { name: "Aprovar" })).toHaveLength(3);
  expect(screen.getAllByRole("button", { name: "Rejeitar" })).toHaveLength(3);
});

test("results handle backend authorization and no-vote states", async () => {
  vi.mocked(getStudentResults).mockRejectedValue(
    new ApiError("Os resultados estarão disponíveis após o encerramento.", 409),
  );
  render(<StudentResultsPage />);
  expect(await screen.findByText("Os resultados estarão disponíveis após o encerramento.")).toBeTruthy();

  cleanup();
  vi.mocked(getStudentResults).mockResolvedValue({
    status: "no_votes",
    total_votes: 0,
    proposals: [],
    winners: [],
    is_tie: false,
    survey_results: { respondent_count: 0, questions: [] },
    compatibility_status: "insufficient_data",
    compatibility_winners: [],
    compatibility_is_tie: false,
  });
  render(<StudentResultsPage />);
  expect(await screen.findByText(/ainda não há votos registrados/)).toBeTruthy();
  expect(screen.getByText(/não há respostas suficientes para calcular compatibilidade/i))
    .toBeTruthy();
  expect(screen.getByRole("heading", {
    name: "Camisa com maior compatibilidade",
  })).toBeTruthy();
  expect(screen.getByText(/não há respostas suficientes para identificar a camisa/i))
    .toBeTruthy();
});

test("results emphasize winners and render existing percentages as meters", async () => {
  vi.mocked(getStudentResults).mockResolvedValue({
    status: "complete",
    total_votes: 10,
    proposals: [
      {
        proposal_id: 1,
        title: "Proposta vencedora",
        image_url: "/proposals/1/image/",
        vote_count: 6,
        percentage: 60,
        compatibility: 80,
        valid_assessments: 5,
      },
      {
        proposal_id: 2,
        title: "Outra proposta",
        image_url: "/proposals/2/image/",
        vote_count: 4,
        percentage: 40,
        compatibility: 92,
        valid_assessments: 5,
      },
    ],
    winners: [{
      proposal_id: 1,
      title: "Proposta vencedora",
      vote_count: 6,
      percentage: 60,
    }],
    is_tie: false,
    survey_results: {
      respondent_count: 10,
      questions: [
        {
          id: "base_color",
          question: "Cor-base preferida",
          respondent_count: 10,
          options: [
            { value: "BLACK", label: "Preta", response_count: 6, percentage: 60 },
            { value: "WHITE", label: "Branca", response_count: 4, percentage: 40 },
          ],
        },
        {
          id: "related_to_course",
          question: "Você gostaria que a camiseta fosse relacionada ao curso?",
          respondent_count: 10,
          options: [
            { value: "true", label: "Sim", response_count: 7, percentage: 70 },
            { value: "false", label: "Não", response_count: 3, percentage: 30 },
          ],
        },
        {
          id: "wants_animal",
          question: "Você gostaria de um tema com animal?",
          respondent_count: 10,
          options: [
            { value: "true", label: "Sim", response_count: 4, percentage: 40 },
            { value: "false", label: "Não", response_count: 6, percentage: 60 },
          ],
        },
        {
          id: "preferred_animal",
          question: "Qual animal?",
          respondent_count: 4,
          options: [
            { value: "Gato", label: "Gato", response_count: 3, percentage: 75 },
          ],
        },
        {
          id: "preferred_colors",
          question: "Quais cores adicionais você prefere? (opcional)",
          respondent_count: 10,
          options: [
            { value: "RED", label: "Vermelho", response_count: 0, percentage: 0 },
            { value: "BLUE", label: "Azul", response_count: 4, percentage: 40 },
            { value: "YELLOW", label: "Amarelo", response_count: 0, percentage: 0 },
            { value: "GREEN", label: "Verde", response_count: 0, percentage: 0 },
            { value: "ORANGE", label: "Laranja", response_count: 0, percentage: 0 },
            { value: "PURPLE", label: "Roxo/Violeta", response_count: 0, percentage: 0 },
            { value: "PINK", label: "Rosa", response_count: 0, percentage: 0 },
            { value: "BROWN", label: "Marrom", response_count: 0, percentage: 0 },
            { value: "BLACK", label: "Preto", response_count: 0, percentage: 0 },
            { value: "WHITE", label: "Branco", response_count: 0, percentage: 0 },
          ],
        },
        {
          id: "wants_collar",
          question: "Você prefere camiseta com gola?",
          respondent_count: 10,
          options: [
            { value: "true", label: "Sim", response_count: 7, percentage: 70 },
            { value: "false", label: "Não", response_count: 3, percentage: 30 },
          ],
        },
        {
          id: "wants_phrase",
          question: "Você gostaria de uma frase na camiseta?",
          respondent_count: 10,
          options: [
            { value: "true", label: "Sim", response_count: 2, percentage: 20 },
            { value: "false", label: "Não", response_count: 8, percentage: 80 },
          ],
        },
        {
          id: "intends_to_buy",
          question: "Você pretende comprar a camiseta?",
          respondent_count: 10,
          options: [
            { value: "true", label: "Sim", response_count: 5, percentage: 50 },
            { value: "false", label: "Não", response_count: 5, percentage: 50 },
          ],
        },
      ],
    },
    compatibility_status: "available",
    compatibility_winners: [{
      proposal_id: 2,
      title: "Outra proposta",
      valid_assessments: 5,
      average_compatibility: 92,
      status: "available",
    }],
    compatibility_is_tie: false,
  });

  render(<StudentResultsPage />);

  expect(await screen.findByText("Mais votada")).toBeTruthy();
  expect(screen.getByText("Maior compatibilidade")).toBeTruthy();
  expect(screen.getAllByText("Proposta vencedora").length).toBeGreaterThan(1);
  const winnerImage = screen.getByRole("img", {
    name: "Fotografia da proposta Proposta vencedora",
  });
  expect(winnerImage.getAttribute("src")).toBe("/proposals/1/image/");
  expect(screen.getByText("6", { selector: ".results-winner-card strong" })).toBeTruthy();
  expect(screen.getByRole("heading", { name: "Resultado da pesquisa" })).toBeTruthy();
  expect(screen.getAllByRole("heading", { level: 3 })).toHaveLength(10);
  expect(screen.getByText("Qual animal?")).toBeTruthy();
  expect(screen.getByText("Marrom")).toBeTruthy();
  expect(screen.getByRole("meter", { name: "Gato: 75%" })).toBeTruthy();
  expect(screen.getAllByRole("meter").length).toBeGreaterThan(20);
  expect(screen.getByRole("meter", {
    name: "Percentual de votos de Proposta vencedora",
  }).getAttribute("aria-valuenow")).toBe("60");
  const zeroResponsesMeter = screen.getByRole("meter", { name: "Vermelho: 0%" });
  expect(zeroResponsesMeter.getAttribute("aria-valuenow")).toBe("0");
  expect(zeroResponsesMeter.closest(".survey-option")?.textContent).toContain("0 respostas");
  expect(screen.getByRole("meter", { name: "Azul: 40%" })
    .getAttribute("aria-valuenow")).toBe("40");
  expect(screen.getByRole("meter", { name: "Compatibilidade de Proposta vencedora" })
    .getAttribute("aria-valuenow")).toBe("80");
  const compatibilitySpotlight = screen.getByRole("region", {
    name: "Camisa com maior compatibilidade",
  });
  const compatibilityImage = within(compatibilitySpotlight).getByRole("img", {
    name: "Fotografia da proposta Outra proposta",
  });
  expect(compatibilityImage.getAttribute("src")).toBe("/proposals/2/image/");
  expect(within(compatibilitySpotlight).getByText("92%")).toBeTruthy();
  expect(within(compatibilitySpotlight).queryByText("Proposta vencedora")).toBeNull();
});

test("compatibility spotlight shows every tied proposal and uses their own photos", async () => {
  vi.mocked(getStudentResults).mockResolvedValue(makeStudentResults({
    proposals: [
      {
        proposal_id: 1,
        title: "Vencedora da votação",
        image_url: "/proposals/1/image/",
        vote_count: 5,
        percentage: 100,
        compatibility: 74,
        valid_assessments: 2,
      },
      {
        proposal_id: 2,
        title: "Empatada em compatibilidade A",
        image_url: "/proposals/2/image/",
        vote_count: 0,
        percentage: 0,
        compatibility: 88.5,
        valid_assessments: 2,
      },
      {
        proposal_id: 3,
        title: "Empatada em compatibilidade B",
        image_url: "/proposals/3/image/",
        vote_count: 0,
        percentage: 0,
        compatibility: 88.5,
        valid_assessments: 2,
      },
    ],
    winners: [{
      proposal_id: 1,
      title: "Vencedora da votação",
      vote_count: 5,
      percentage: 100,
    }],
    compatibility_status: "available",
    compatibility_winners: [
      {
        proposal_id: 2,
        title: "Empatada em compatibilidade A",
        valid_assessments: 2,
        average_compatibility: 88.5,
        status: "available",
      },
      {
        proposal_id: 3,
        title: "Empatada em compatibilidade B",
        valid_assessments: 2,
        average_compatibility: 88.5,
        status: "available",
      },
    ],
    compatibility_is_tie: true,
  }));

  render(<StudentResultsPage />);

  const spotlight = await screen.findByRole("region", {
    name: "Camisa com maior compatibilidade",
  });
  expect(within(spotlight).getByRole("img", {
    name: "Fotografia da proposta Empatada em compatibilidade A",
  }).getAttribute("src")).toBe("/proposals/2/image/");
  expect(within(spotlight).getByRole("img", {
    name: "Fotografia da proposta Empatada em compatibilidade B",
  }).getAttribute("src")).toBe("/proposals/3/image/");
  expect(within(spotlight).getAllByText("88,5%")).toHaveLength(2);
  expect(within(spotlight).queryByText("Vencedora da votação")).toBeNull();
});

test("compatibility spotlight does not invent a result without enough data", async () => {
  vi.mocked(getStudentResults).mockResolvedValue(makeStudentResults());

  render(<StudentResultsPage />);

  expect(await screen.findByText(
    /não há respostas suficientes para identificar a camisa/i,
  )).toBeTruthy();
  expect(screen.queryByRole("img", {
    name: /Fotografia da proposta/,
  })).toBeNull();
});

test("expired student session removes protected content", async () => {
  vi.mocked(getSession)
    .mockResolvedValueOnce({
      authenticated: true,
      user: { id: 5, email: "student@school.test", role: "student" },
    })
    .mockResolvedValueOnce({ authenticated: false });
  vi.mocked(logoutSession).mockResolvedValue({
    authenticated: false,
    message: "Sessão encerrada.",
  });

  render(
    <MemoryRouter initialEntries={["/student"]}>
      <AuthProvider>
        <Routes>
          <Route
            element={
              <RequireStudent>
                <p>Área protegida</p>
              </RequireStudent>
            }
            path="/student"
          />
          <Route element={<p>Entre novamente</p>} path="/login" />
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  );

  expect(await screen.findByText("Área protegida")).toBeTruthy();
  fireEvent(window, new Event("threeshirt:session-expired"));
  expect(await screen.findByText("Entre novamente")).toBeTruthy();
  expect(logoutSession).not.toHaveBeenCalled();
});
