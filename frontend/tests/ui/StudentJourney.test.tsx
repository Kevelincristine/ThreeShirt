import { afterEach, beforeAll, beforeEach, expect, test, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
import { StudentPreferencesPage } from "../../src/pages/StudentPreferencesPage";
import { StudentProposalsPage } from "../../src/pages/StudentProposalsPage";
import { StudentResultsPage } from "../../src/pages/StudentResultsPage";
import {
  ApiError,
  castStudentVote,
  confirmAdminMaintenance,
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

test("an admin can navigate between the student and admin areas in one session", async () => {
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
              element={
                <RequireStudent allowAdmin>
                  <p>Painel do estudante protegido</p>
                </RequireStudent>
              }
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

  await screen.findByRole("link", { name: "Abrir painel administrativo" });
  await user.click(screen.getByRole("link", { name: "Abrir painel do estudante" }));
  expect(await screen.findByText("Painel do estudante protegido")).toBeTruthy();

  await user.click(screen.getByRole("link", { name: "Administração" }));
  expect(await screen.findByText("Painel administrativo protegido")).toBeTruthy();
  expect(getSession).toHaveBeenCalledTimes(1);
});

test("a student administrator sees both menus and can enter both protected areas", async () => {
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
              path="student/preferences"
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

  expect(await screen.findByText(/estudante e administrador/)).toBeTruthy();
  expect(screen.getByRole("link", { name: "Propostas" })).toBeTruthy();
  expect(screen.getByRole("link", { name: "Administração" })).toBeTruthy();
  await user.click(screen.getByRole("link", { name: "Preferências" }));
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

test("admin user management offers all roles and shows account activation state", async () => {
  const user = userEvent.setup();
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
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
    },
    {
      id: 45,
      email: "hybrid@school.test",
      full_name: "Estudante Admin",
      role: "STUDENT_ADMIN",
      is_active: false,
      is_staff: false,
    },
  ]);
  vi.mocked(updateAdminUserRole).mockResolvedValue({ message: "Atualizado." });

  render(
    <MemoryRouter>
      <AuthProvider>
        <AdminDashboardPage />
      </AuthProvider>
    </MemoryRouter>,
  );

  expect(await screen.findByText("Conta ativa")).toBeTruthy();
  expect(screen.getByRole("heading", { name: "Zona de Manutenção" })).toBeTruthy();
  expect(screen.getByText("Conta inativa")).toBeTruthy();
  expect(screen.getByText("Estudante e administrador")).toBeTruthy();
  await user.click(screen.getByRole("button", {
    name: "Definir: Estudante e administrador",
  }));
  await waitFor(() => {
    expect(updateAdminUserRole).toHaveBeenCalledWith(44, "STUDENT_ADMIN");
  });
  confirm.mockRestore();
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
  expect(screen.queryByRole("heading", { name: "Zona de Manutenção" })).toBeNull();
  expect(screen.queryByRole("button", { name: /reinicialização eleitoral/i })).toBeNull();
});

test("admin maintenance requires a preview before the final account deletion", async () => {
  const user = userEvent.setup();
  vi.spyOn(window, "confirm").mockReturnValue(true);
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
    },
  ]);
  vi.mocked(previewAdminMaintenance).mockResolvedValue({
    challenge_token: "one-time-token",
    expires_at: "2026-10-31T12:00:00-03:00",
    action: "DELETE_USER",
    target: {
      id: 44,
      label: "Estudante",
      email: "student@school.test",
    },
    summary: {
      accounts: 1,
      profiles: 1,
      preference_responses: 1,
      proposals: 1,
      votes: 2,
      proposal_images: 1,
    },
    warning: "",
  });
  vi.mocked(confirmAdminMaintenance).mockResolvedValue({
    message: "Conta e registros relacionados excluídos.",
    affected: { accounts: 1, proposals: 1, votes: 2 },
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
  await user.click(screen.getByRole("button", { name: "Revisar exclusão da conta" }));
  await waitFor(() => {
    expect(previewAdminMaintenance).toHaveBeenCalledWith("delete_user", 44);
  });
  expect(await screen.findByText("Votos excluídos")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Confirmar definitivamente" })).toBeTruthy();
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

  expect(await screen.findByText("Pesquisa de preferências")).toBeTruthy();
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

  expect(screen.getByText("Carregando suas preferências...")).toBeTruthy();
});

test("preferences load from API, save through API, and show success", async () => {
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
  await screen.findByRole("heading", { name: "Suas preferências" });
  fireEvent.change(
    screen.getByLabelText("Você gostaria que a camiseta fosse relacionada ao curso?"),
    { target: { value: "true" } },
  );
  fireEvent.click(screen.getByLabelText("Azul"));
  fireEvent.change(
    screen.getByLabelText("Você prefere camiseta com gola?"),
    { target: { value: "true" } },
  );
  fireEvent.change(
    screen.getByLabelText("Você pretende comprar a camiseta?"),
    { target: { value: "true" } },
  );
  fireEvent.submit(
    screen.getByRole("button", { name: "Enviar respostas" }).closest("form")!,
  );

  expect(saveStudentPreferences).toHaveBeenCalledWith(preferences);
  expect(await screen.findByText("Preferências salvas com sucesso.")).toBeTruthy();
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

  expect(await screen.findByText(/A pesquisa está fechada/)).toBeTruthy();
  expect(screen.getByRole("button", { name: "Atualizar respostas" }).hasAttribute("disabled")).toBe(true);
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

test("gallery treats an empty approved list as an explicit empty state", async () => {
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
});

test("admin gallery displays approved designs without offering a vote", async () => {
  vi.mocked(getSession).mockResolvedValue({
    authenticated: true,
    user: { id: 8, email: "admin@school.test", role: "admin" },
  });
  vi.mocked(getApprovedStudentProposals).mockResolvedValue([{
    id: 4,
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
    compatibility_status: "insufficient_data",
    compatibility_winners: [],
    compatibility_is_tie: false,
  });
  render(<StudentResultsPage />);
  expect(await screen.findByText(/ainda não há votos registrados/)).toBeTruthy();
  expect(screen.getByText(/Ainda não há respostas suficientes/)).toBeTruthy();
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
