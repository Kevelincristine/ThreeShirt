import { useEffect, useMemo, useState } from "react";
import { Alert } from "../components/feedback/Alert";
import { Loading } from "../components/feedback/Loading";
import { AccountPasswordForm } from "../components/AccountPasswordForm";
import {
  getAdminDashboard,
  getAdminProposals,
  getAdminStages,
  getAdminUsers,
  getAdminUserHistory,
  previewAdminMaintenance,
  confirmAdminMaintenance,
  reviewAdminProposal,
  updateAdminStage,
  updateAdminUserRole,
  updateAdminUserStatus,
} from "../services/api";
import type {
  AdminDashboardResponse,
  AdminMaintenanceAction,
  AdminMaintenancePreview,
  AdminProposal,
  AdminUser,
  AdminUserHistory,
} from "../types/api";
import { useAuth } from "../app/AuthContext";

const roleLabels: Record<AdminUser["role"], string> = {
  STUDENT: "Estudante",
  STUDENT_ADMIN: "Estudante e administrador",
  ADMIN: "Administrador",
};

const maintenanceCountLabels: Record<string, string> = {
  account_preserved: "Contas preservadas",
  email_preserved: "E-mails preservados",
  profiles_reset: "Perfis reinicializados",
  responses_preserved: "Respostas históricas preservadas",
  responses_removed: "Respostas removidas nesta reinicialização integral",
  votes_preserved: "Votos históricos preservados",
  votes_removed: "Votos removidos nesta reinicialização integral",
  proposals_preserved: "Propostas preservadas",
  proposals_removed: "Propostas removidas nesta reinicialização integral",
  proposals_with_external_votes_preserved: "Propostas mantidas por votos de terceiros",
  external_votes_preserved: "Votos de terceiros preservados",
  participation_identities_preserved: "Identidades de participação preservadas",
  participation_audit_events_preserved: "Eventos de participação preservados",
  account_audit_events_preserved: "Auditorias administrativas preservadas",
  result_snapshots_preserved: "Snapshots de resultados preservados",
  maintenance_audit_events_preserved: "Auditorias de manutenção preservadas",
  pending_replacement_authorizations_removed: "Autorizações pendentes removidas",
  votes_from_other_participants: "Votos de outros participantes afetados",
  accounts: "Contas que serão removidas",
  profiles: "Perfis que serão removidos",
  preference_responses: "Respostas que serão removidas",
  votes: "Votos que serão removidos",
  proposals: "Propostas que serão removidas",
  files_to_remove: "Imagens a remover",
};

const maintenanceActionLabels: Record<AdminMaintenancePreview["action"], string> = {
  RESET_USER_BASIC: "Reinicialização da conta com histórico preservado",
  RESET_USER_FULL: "Reinicialização integral da participação",
  DELETE_USER: "Exclusão de conta",
  DELETE_PROPOSAL: "Exclusão de proposta",
  RESET_ELECTION: "Reinicialização eleitoral",
};

const proposalStatusLabels = {
  PENDING: "Aguardando aprovação",
  APPROVED: "Aprovada",
  REJECTED: "Rejeitada",
};

const preferenceColorLabels: Record<string, string> = {
  RED: "Vermelho",
  BLUE: "Azul",
  YELLOW: "Amarelo",
  GREEN: "Verde",
  ORANGE: "Laranja",
  PURPLE: "Roxo/violeta",
  PINK: "Rosa",
  BROWN: "Marrom",
  BLACK: "Preto",
  WHITE: "Branco",
};

function yesNo(value: boolean): string {
  return value ? "Sim" : "Não";
}

function formatPercent(value: number | null): string {
  if (value == null) return "—";
  return `${value.toFixed(1)}%`;
}

export function AdminDashboardPage() {
  const { state: authState } = useAuth();
  const currentUserId =
    authState.status === "authenticated" ? authState.user.id : null;
  const canMaintain =
    authState.status === "authenticated" && authState.user.role === "admin";
  const canManageUsers =
    authState.status === "authenticated" &&
    (authState.user.role === "admin" ||
      authState.user.role === "student_admin");
  const [state, setState] = useState<
    | { status: "loading" }
    | {
        status: "ready";
        dashboard: AdminDashboardResponse;
        proposals: AdminProposal[];
        stages: {
          survey_open: boolean;
          proposal_submission_open: boolean;
          voting_open: boolean;
          results_release_authorized: boolean;
        };
        users: AdminUser[];
      }
    | { status: "error"; message: string }
  >({ status: "loading" });

  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const [selectedMaintenanceUser, setSelectedMaintenanceUser] = useState("");
  const [selectedMaintenanceProposal, setSelectedMaintenanceProposal] = useState("");
  const [maintenancePreview, setMaintenancePreview] =
    useState<AdminMaintenancePreview | null>(null);
  const [maintenanceReason, setMaintenanceReason] = useState("");
  const [maintenanceConfirmationPhrase, setMaintenanceConfirmationPhrase] =
    useState("");
  const [maintenanceAcknowledged, setMaintenanceAcknowledged] = useState(false);
  const [selectedHistory, setSelectedHistory] =
    useState<AdminUserHistory | null>(null);
  const [maintenanceNotice, setMaintenanceNotice] = useState<{
    variant: "success" | "error";
    message: string;
  } | null>(null);
  const loadData = async (): Promise<void> => {
    try {
      const [dashboard, proposals, stages, users] = await Promise.all([
        getAdminDashboard(),
        getAdminProposals(),
        getAdminStages(),
        getAdminUsers(),
      ]);
      setState({
        status: "ready",
        dashboard,
        proposals,
        stages,
        users,
      });
    } catch (error) {
      setState({
        status: "error",
        message:
          error instanceof Error
            ? error.message
            : "Não foi possível carregar a administração.",
      });
    }
  };

  useEffect(() => {
    void loadData();
  }, [canMaintain]);

  const pendingProposals = useMemo(
    () => state.status === "ready" ? state.proposals.filter((proposal) => proposal.status === "PENDING") : [],
    [state],
  );

  async function handleProposalReview(proposalId: number, decision: "approve" | "reject") {
    if (pendingAction) return;
    const result = window.confirm(
      decision === "approve"
        ? "Confirmar a aprovação desta proposta?"
        : "Confirmar a rejeição desta proposta?",
    );
    if (!result) return;

    const reason = decision === "reject"
      ? (window.prompt("Informe o motivo da rejeição (opcional):", "") ?? "").trim()
      : "";

    setPendingAction(`proposal:${proposalId}:${decision}`);
    try {
      await reviewAdminProposal(proposalId, decision, reason || (decision === "reject" ? "Motivo não informado." : ""));
      await loadData();
    } catch (error) {
      setState({
        status: "error",
        message: error instanceof Error ? error.message : "Não foi possível atualizar a proposta.",
      });
    } finally {
      setPendingAction(null);
    }
  }

  async function handleStageToggle(
    stage: "survey" | "proposal_submission" | "voting" | "results_release",
    action: "open" | "close",
  ) {
    if (pendingAction) return;
    const actionLabel = action === "open" ? "abrir" : "encerrar";
    const stageLabel =
      stage === "survey"
        ? "pesquisa"
        : stage === "proposal_submission"
          ? "envio de propostas"
          : stage === "voting" ? "votação" : "divulgação dos resultados";
    if (!window.confirm(`Confirmar ${actionLabel} a ${stageLabel}?`)) return;

    setPendingAction(`stage:${stage}:${action}`);
    try {
      await updateAdminStage(stage, action);
      await loadData();
    } catch (error) {
      setState({
        status: "error",
        message: error instanceof Error ? error.message : "Não foi possível atualizar a etapa.",
      });
    } finally {
      setPendingAction(null);
    }
  }

  async function handleUserRole(userId: number, role: AdminUser["role"]) {
    if (pendingAction) return;
    const reason = window.prompt(
      `Informe a justificativa para alterar o papel para ${roleLabels[role]}:`,
    )?.trim();
    if (!reason) return;

    setPendingAction(`role:${userId}:${role}`);
    try {
      await updateAdminUserRole(userId, role, reason);
      await loadData();
    } catch (error) {
      setState({
        status: "error",
        message: error instanceof Error ? error.message : "Não foi possível atualizar a permissão.",
      });
    } finally {
      setPendingAction(null);
    }
  }

  async function handleUserStatus(userId: number, isActive: boolean) {
    if (pendingAction) return;
    const reason = window.prompt(
      `Informe a justificativa para ${isActive ? "reativar" : "bloquear"} esta conta:`,
    )?.trim();
    if (!reason) return;

    setPendingAction(`status:${userId}:${String(isActive)}`);
    try {
      await updateAdminUserStatus(userId, isActive, reason);
      await loadData();
    } catch (error) {
      setState({
        status: "error",
        message: error instanceof Error ? error.message : "Não foi possível atualizar o acesso.",
      });
    } finally {
      setPendingAction(null);
    }
  }

  async function beginMaintenance(
    action: AdminMaintenanceAction,
    targetId?: number,
  ) {
    if (pendingAction) return;
    if (!maintenanceReason.trim()) {
      setMaintenanceNotice({
        variant: "error",
        message: "Informe a justificativa antes de gerar a prévia.",
      });
      return;
    }

    setPendingAction(`maintenance:preview:${action}`);
    setMaintenancePreview(null);
    setMaintenanceAcknowledged(false);
    setMaintenanceConfirmationPhrase("");
    setMaintenanceNotice(null);
    try {
      const preview = await previewAdminMaintenance(
        action,
        targetId,
        maintenanceReason.trim(),
      );
      setMaintenancePreview(preview);
    } catch (error) {
      setMaintenanceNotice({
        variant: "error",
        message:
          error instanceof Error
            ? error.message
            : "Não foi possível preparar a prévia da manutenção.",
      });
    } finally {
      setPendingAction(null);
    }
  }

  async function confirmMaintenance() {
    if (!maintenancePreview || pendingAction) return;
    setPendingAction("maintenance:confirm");
    setMaintenanceNotice(null);
    try {
      const result = await confirmAdminMaintenance(
        maintenancePreview.challenge_token,
        maintenanceConfirmationPhrase,
      );
      const notices = [
        result.message,
        result.warning,
        result.backup
          ? `Backup verificado: ${result.backup.path} (SHA-256 ${result.backup.sha256}).`
          : "",
      ].filter(Boolean);
      if (result.file_cleanup_pending) {
        notices.push(
          "A remoção de uma ou mais imagens está pendente e requer nova tentativa administrativa.",
        );
      }

      setMaintenanceNotice({
        variant: "success",
        message: notices.join(" "),
      });
      setMaintenancePreview(null);
      setMaintenanceAcknowledged(false);
      setMaintenanceConfirmationPhrase("");
      await loadData();
    } catch (error) {
      setMaintenancePreview(null);
      setMaintenanceAcknowledged(false);
      setMaintenanceConfirmationPhrase("");
      setMaintenanceNotice({
        variant: "error",
        message:
          error instanceof Error
            ? error.message
            : "Não foi possível concluir a manutenção.",
      });
    } finally {
      setPendingAction(null);
    }
  }

  async function handleUserHistory(user: AdminUser) {
    if (pendingAction) return;
    if (selectedHistory?.user_id === user.id) {
      setSelectedHistory(null);
      return;
    }
    setPendingAction(`history:${user.id}`);
    try {
      setSelectedHistory(await getAdminUserHistory(user.id));
    } catch (error) {
      setMaintenanceNotice({
        variant: "error",
        message:
          error instanceof Error
            ? error.message
            : "Não foi possível consultar o histórico da conta.",
      });
    } finally {
      setPendingAction(null);
    }
  }

  if (state.status === "loading") {
    return (
      <section className="paper-panel admin-panel">
        <h1>Painel administrativo</h1>
        <Loading label="Carregando dados do sistema..." />
      </section>
    );
  }

  if (state.status === "error") {
    return (
      <section className="paper-panel admin-panel">
        <h1>Painel administrativo</h1>
        <Alert variant="error">{state.message}</Alert>
        <button className="button button--primary" type="button" onClick={() => void loadData()}>
          Tentar novamente
        </button>
      </section>
    );
  }

  const { dashboard, proposals, stages, users } = state;

  return (
    <section aria-labelledby="admin-dashboard-title" className="paper-panel admin-panel">
      <div className="admin-header">
        <div>
          <p className="eyebrow">Administração</p>
          <h1 id="admin-dashboard-title">Painel administrativo</h1>
        </div>
        <button className="button button--secondary" type="button" onClick={() => void loadData()} disabled={Boolean(pendingAction)}>
          Atualizar dados
        </button>
      </div>

      <div className="admin-grid admin-grid--stats">
        <article className="admin-card">
          <h2>Propostas</h2>
          <dl>
            <div><dt>Pendentes</dt><dd>{dashboard.counts.proposals_pending}</dd></div>
            <div><dt>Aprovadas</dt><dd>{dashboard.counts.proposals_approved}</dd></div>
            <div><dt>Rejeitadas</dt><dd>{dashboard.counts.proposals_rejected}</dd></div>
          </dl>
        </article>
        <article className="admin-card">
          <h2>Etapas</h2>
          <dl>
            <div><dt>Pesquisa</dt><dd>{stages.survey_open ? "Aberta" : "Fechada"}</dd></div>
            <div><dt>Envio</dt><dd>{stages.proposal_submission_open ? "Aberto" : "Fechado"}</dd></div>
            <div><dt>Votação</dt><dd>{stages.voting_open ? "Aberta" : "Fechada"}</dd></div>
            <div><dt>Resultados</dt><dd>{stages.results_release_authorized ? "Liberados" : "Bloqueados"}</dd></div>
          </dl>
        </article>
        <article className="admin-card">
          <h2>Participação</h2>
          <dl>
            <div><dt>Alunos ativos</dt><dd>{dashboard.counts.students_total}</dd></div>
            <div><dt>Respostas</dt><dd>{dashboard.progress.survey_completed}</dd></div>
            <div><dt>Percentual</dt><dd>{formatPercent(dashboard.progress.survey_participation)}</dd></div>
          </dl>
        </article>
        <article className="admin-card">
          <h2>Votação</h2>
          <dl>
            <div><dt>Votos</dt><dd>{dashboard.counts.votes_cast}</dd></div>
            <div><dt>Resultados</dt><dd>{stages.results_release_authorized ? "Liberados" : "Bloqueados"}</dd></div>
            <div><dt>Administradores</dt><dd>{dashboard.counts.admins_total}</dd></div>
          </dl>
        </article>
      </div>

      <div className="admin-grid admin-grid--controls">
        <article className="admin-card">
          <h2>Gerenciar etapas</h2>
          <div className="admin-actions">
            <button className="button button--secondary" type="button" disabled={Boolean(pendingAction)} onClick={() => void handleStageToggle("survey", stages.survey_open ? "close" : "open")}>
              {stages.survey_open ? "Encerrar pesquisa" : "Abrir pesquisa"}
            </button>
            <button className="button button--secondary" type="button" disabled={Boolean(pendingAction)} onClick={() => void handleStageToggle("proposal_submission", stages.proposal_submission_open ? "close" : "open")}>
              {stages.proposal_submission_open ? "Fechar envio" : "Abrir envio"}
            </button>
            <button className="button button--secondary" type="button" disabled={Boolean(pendingAction)} onClick={() => void handleStageToggle("voting", stages.voting_open ? "close" : "open")}>
              {stages.voting_open ? "Encerrar votação" : "Abrir votação"}
            </button>
            <button className="button button--secondary" type="button" disabled={Boolean(pendingAction)} onClick={() => void handleStageToggle("results_release", stages.results_release_authorized ? "close" : "open")}>
              {stages.results_release_authorized ? "Bloquear resultados" : "Liberar resultados"}
            </button>
          </div>
        </article>

        <article className="admin-card">
          <h2>Resumo rápido</h2>
          <p>Há {pendingProposals.length} propostas pendentes aguardando revisão.</p>
          <p>Usuários ativos: {users.filter((user) => user.is_active).length}</p>
          <p>Faltam {Math.max(dashboard.counts.students_total - dashboard.progress.survey_completed, 0)} respostas de pesquisa.</p>
        </article>
      </div>

      <div className="admin-section">
        <h2>Propostas para revisão</h2>
        {proposals.length === 0 ? (
          <p>Nenhuma proposta registrada.</p>
        ) : (
          <div className="admin-list">
            {proposals.map((proposal) => (
              <article key={proposal.id} className="admin-item">
                <div className="admin-item__meta">
                  <strong>{proposal.title}</strong>
                  <span>{proposal.status}</span>
                </div>
                <p>{proposal.description}</p>
                <p>Autor: {proposal.author.full_name || proposal.author.email}</p>
                <section aria-label={`Respostas da pesquisa de ${proposal.author.full_name || proposal.author.email}`}>
                  <h3>Respostas da pesquisa de preferências</h3>
                  {proposal.preferences ? (
                    <dl className="admin-proposal-preferences">
                      <div>
                        <dt>Cor principal</dt>
                        <dd>{proposal.preferences.base_color === "BLACK" ? "Preta" : "Branca"}</dd>
                      </div>
                      <div>
                        <dt>Curso</dt>
                        <dd>{yesNo(proposal.preferences.related_to_course)}</dd>
                      </div>
                      <div>
                        <dt>Tema com animais</dt>
                        <dd>{yesNo(proposal.preferences.wants_animal)}</dd>
                      </div>
                      <div>
                        <dt>Animal escolhido</dt>
                        <dd>
                          {proposal.preferences.wants_animal
                            ? proposal.preferences.preferred_animal.trim() || "Não respondida"
                            : "Não se aplica"}
                        </dd>
                      </div>
                      <div>
                        <dt>Cores adicionais</dt>
                        <dd>
                          {proposal.preferences.preferred_colors.length > 0
                            ? proposal.preferences.preferred_colors
                                .map((color) => preferenceColorLabels[color] ?? color)
                                .join(", ")
                            : "Não respondida"}
                        </dd>
                      </div>
                      <div>
                        <dt>Modelo de gola</dt>
                        <dd>{yesNo(proposal.preferences.wants_collar)}</dd>
                      </div>
                      <div>
                        <dt>Frase escolhida</dt>
                        <dd>
                          O formulário registra apenas se deseja uma frase:{" "}
                          {yesNo(proposal.preferences.wants_phrase)}.
                        </dd>
                      </div>
                      <div>
                        <dt>Intenção de compra</dt>
                        <dd>{yesNo(proposal.preferences.intends_to_buy)}</dd>
                      </div>
                    </dl>
                  ) : (
                    <p>Este estudante ainda não preencheu a pesquisa de preferências.</p>
                  )}
                </section>
                {proposal.rejection_reason && proposal.status === "REJECTED" && (
                  <p>Motivo: {proposal.rejection_reason}</p>
                )}
                <a href={proposal.image_url} target="_blank" rel="noreferrer">Abrir imagem</a>
                {proposal.status === "PENDING" && (
                  <div className="admin-actions admin-actions--inline">
                    <button
                      className="button button--primary"
                      type="button"
                      disabled={Boolean(pendingAction)}
                      onClick={() => void handleProposalReview(proposal.id, "approve")}
                    >
                      Aprovar
                    </button>
                    <button
                      className="button button--secondary"
                      type="button"
                      disabled={Boolean(pendingAction)}
                      onClick={() => void handleProposalReview(proposal.id, "reject")}
                    >
                      Rejeitar
                    </button>
                  </div>
                )}
              </article>
            ))}
          </div>
        )}
      </div>

      <AccountPasswordForm />

      <div className="admin-section">
        <h2>Usuários e permissões</h2>
        <div className="admin-list">
          {users.map((user) => (
            <article key={user.id} className="admin-item admin-item--compact">
              <div className="admin-item__meta">
                <strong>{user.full_name || user.email}</strong>
                <span>{roleLabels[user.role]}</span>
              </div>
              <p>{user.email}</p>
              <p>Conta {user.is_active ? "ativa" : "inativa"}</p>
              <p>
                Senha {user.has_password ? "configurada" : "ainda não configurada"} ·
                {user.has_participation
                  ? " possui histórico eleitoral"
                  : " sem participação registrada"}
              </p>
              <div className="admin-actions admin-actions--inline">
                {(
                  canMaintain
                    ? ["STUDENT", "STUDENT_ADMIN", "ADMIN"] as const
                    : ["STUDENT", "STUDENT_ADMIN"] as const
                ).filter((role) => role !== user.role).map((role) => (
                  <button
                    className="button button--secondary"
                    key={role}
                    type="button"
                    disabled={
                      Boolean(pendingAction) ||
                      (!canMaintain && user.role === "ADMIN")
                    }
                    onClick={() => void handleUserRole(user.id, role)}
                  >
                    {`Definir: ${roleLabels[role]}`}
                  </button>
                ))}
                <button
                  className="button button--secondary"
                  type="button"
                  disabled={
                    Boolean(pendingAction) ||
                    (!canMaintain && user.role === "ADMIN")
                  }
                  onClick={() => void handleUserStatus(user.id, !user.is_active)}
                >
                  {user.is_active ? "Desativar" : "Ativar"}
                </button>
                <button
                  className="button button--secondary"
                  type="button"
                  disabled={
                    Boolean(pendingAction) ||
                    (!canMaintain && user.role === "ADMIN")
                  }
                  onClick={() => void handleUserHistory(user)}
                >
                  {selectedHistory?.user_id === user.id
                    ? "Ocultar histórico"
                    : "Consultar histórico"}
                </button>
              </div>
              {selectedHistory?.user_id === user.id && (
                <section aria-label={`Histórico de ${user.email}`}>
                  <h3>Participações por geração</h3>
                  {selectedHistory.participations.length === 0 ? (
                    <p>Nenhuma participação histórica registrada.</p>
                  ) : (
                    <ul>
                      {selectedHistory.participations.map((participation) => (
                        <li key={participation.identity_id}>
                          Geração {participation.generation} —{" "}
                          {participation.current ? "atual" : "encerrada"};{" "}
                          {participation.responses} respostas,{" "}
                          {participation.votes} votos,{" "}
                          {participation.proposals} propostas.
                        </li>
                      ))}
                    </ul>
                  )}
                  <h3>Auditoria de participações</h3>
                  {selectedHistory.participation_audit.length === 0 ? (
                    <p>Nenhuma ação eleitoral auditada.</p>
                  ) : (
                    <ul>
                      {selectedHistory.participation_audit.map((event) => (
                        <li key={`participation-${event.id}`}>
                          {event.action} — {event.reason} (administrador{" "}
                          {event.actor_id}, {new Date(event.created_at).toLocaleString()})
                        </li>
                      ))}
                    </ul>
                  )}
                  <h3>Auditoria da conta</h3>
                  {selectedHistory.account_audit.length === 0 ? (
                    <p>Nenhuma ação administrativa da conta.</p>
                  ) : (
                    <ul>
                      {selectedHistory.account_audit.map((event) => (
                        <li key={`account-${event.id}`}>
                          {event.action} — {event.reason} (administrador{" "}
                          {event.actor_id}, {new Date(event.created_at).toLocaleString()})
                        </li>
                      ))}
                    </ul>
                  )}
                </section>
              )}
            </article>
          ))}
        </div>
      </div>

      {canManageUsers && (
        <section
          aria-labelledby="maintenance-zone-title"
          className="admin-section maintenance-zone"
        >
          <div className="maintenance-zone__header">
            <p className="eyebrow">Ações irreversíveis</p>
            <h2 id="maintenance-zone-title">Zona de Manutenção</h2>
            <p>
              Cada operação exige justificativa, prévia detalhada e confirmação
              explícita. O backend cria e verifica um backup SQLite antes de
              executar; as prévias expiram após 10 minutos.
            </p>
          </div>

          {maintenanceNotice && (
            <Alert variant={maintenanceNotice.variant}>
              {maintenanceNotice.message}
            </Alert>
          )}

          <div className="maintenance-grid">
            <article className="maintenance-card">
              <h3>Reinicializar uma conta</h3>
              <p>
                Mantém o e-mail e o username, remove a senha e redefine o perfil
                para estudante. A reinicialização básica preserva as
                participações eleitorais históricas.
              </p>
              <label className="form-field" htmlFor="maintenance-user">
                Conta
                <select
                  id="maintenance-user"
                  value={selectedMaintenanceUser}
                  onChange={(event) => {
                    setSelectedMaintenanceUser(event.target.value);
                    setMaintenancePreview(null);
                    setMaintenanceNotice(null);
                  }}
                  disabled={Boolean(pendingAction)}
                >
                  <option value="">Selecione uma conta</option>
                  {users.map((user) => (
                    <option
                      key={user.id}
                      value={user.id}
                      disabled={
                        user.id === currentUserId ||
                        (!canMaintain && user.role === "ADMIN")
                      }
                    >
                      {user.full_name || user.email} — {roleLabels[user.role]}
                      {user.id === currentUserId ? " (sua conta)" : ""}
                    </option>
                  ))}
                </select>
              </label>
              <button
                className="button button--secondary"
                type="button"
                disabled={!selectedMaintenanceUser || Boolean(pendingAction)}
                onClick={() =>
                  void beginMaintenance(
                    "reset_user_basic",
                    Number(selectedMaintenanceUser),
                  )
                }
              >
                Revisar reinicialização preservando histórico
              </button>
              <button
                className="button button--danger"
                type="button"
                disabled={!selectedMaintenanceUser || Boolean(pendingAction)}
                onClick={() =>
                  void beginMaintenance(
                    "delete_user",
                    Number(selectedMaintenanceUser),
                  )
                }
              >
                Revisar exclusão da conta
              </button>
              {canMaintain && (
                <button
                  className="button button--danger"
                  type="button"
                  disabled={!selectedMaintenanceUser || Boolean(pendingAction)}
                  onClick={() =>
                    void beginMaintenance(
                      "reset_user_full",
                      Number(selectedMaintenanceUser),
                    )
                  }
                >
                  Revisar reinicialização integral
                </button>
              )}
            </article>
            {canMaintain && (
              <>
                <article className="maintenance-card">
                  <h3>Gerenciar propostas</h3>
                  <p>
                    A prévia informa o status, os votos e os registros
                    relacionados. Snapshots publicados e auditorias são
                    preservados.
                  </p>
                  <label className="form-field" htmlFor="maintenance-proposal">
                    Proposta
                    <select
                      id="maintenance-proposal"
                      value={selectedMaintenanceProposal}
                      onChange={(event) => {
                        setSelectedMaintenanceProposal(event.target.value);
                        setMaintenancePreview(null);
                        setMaintenanceNotice(null);
                      }}
                      disabled={Boolean(pendingAction)}
                    >
                      <option value="">Selecione uma proposta</option>
                      {proposals.map((proposal) => (
                        <option key={proposal.id} value={proposal.id}>
                          {proposal.title} — {proposalStatusLabels[proposal.status]}
                        </option>
                      ))}
                    </select>
                  </label>
                  <button
                    className="button button--danger"
                    type="button"
                    disabled={!selectedMaintenanceProposal || Boolean(pendingAction)}
                    onClick={() =>
                      void beginMaintenance(
                        "delete_proposal",
                        Number(selectedMaintenanceProposal),
                      )
                    }
                  >
                    Revisar exclusão da proposta
                  </button>
                </article>
                <article className="maintenance-card">
                  <h3>Manutenção eleitoral</h3>
                  <p>
                    Reinicia somente dados operacionais da eleição atual.
                    Identidades, eventos de auditoria, snapshots publicados e
                    histórico de manutenção permanecem.
                  </p>
                  <button
                    className="button button--danger"
                    type="button"
                    disabled={Boolean(pendingAction)}
                    onClick={() => void beginMaintenance("reset_election")}
                  >
                    Revisar reinicialização eleitoral
                  </button>
                </article>
              </>
            )}
          </div>

          <label className="form-field" htmlFor="maintenance-reason">
            Justificativa obrigatória
            <textarea
              id="maintenance-reason"
              maxLength={1000}
              onChange={(event) => setMaintenanceReason(event.target.value)}
              value={maintenanceReason}
              required
            />
          </label>

          {maintenancePreview && (
            <div
              aria-labelledby="maintenance-preview-title"
              className="maintenance-preview"
              role="region"
            >
              <h3 id="maintenance-preview-title">
                Prévia: {maintenanceActionLabels[maintenancePreview.action]}
              </h3>
              <p>
                Alvo: <strong>{maintenancePreview.target.label}</strong>
                {maintenancePreview.target.email
                  ? ` (${maintenancePreview.target.email})`
                  : ""}
                {maintenancePreview.target.status
                  ? ` — ${proposalStatusLabels[maintenancePreview.target.status]}`
                  : ""}
                {maintenancePreview.target.author
                  ? ` — autoria: ${maintenancePreview.target.author}`
                  : ""}
              </p>
              <dl className="maintenance-preview__counts">
                {Object.entries(maintenancePreview.summary).map(([key, count]) => (
                  <div key={key}>
                    <dt>{maintenanceCountLabels[key] ?? key}</dt>
                    <dd>{count}</dd>
                  </div>
                ))}
              </dl>
              {maintenancePreview.warning && (
                <Alert variant="warning">{maintenancePreview.warning}</Alert>
              )}
              {maintenancePreview.restored_settings && (
                <p className="maintenance-preview__defaults">
                  Estado restaurado: pesquisa fechada, envio de propostas
                  aberto, votação fechada, resultados bloqueados e peso de
                  compatibilidade 50.
                </p>
              )}
              {maintenancePreview.action === "RESET_ELECTION" && (
                <label className="form-field" htmlFor="maintenance-confirmation-phrase">
                  Digite exatamente REINICIAR ELEIÇÃO
                  <input
                    id="maintenance-confirmation-phrase"
                    onChange={(event) =>
                      setMaintenanceConfirmationPhrase(event.target.value)
                    }
                    value={maintenanceConfirmationPhrase}
                    autoComplete="off"
                  />
                </label>
              )}
              <label className="form-field">
                <input
                  type="checkbox"
                  checked={maintenanceAcknowledged}
                  onChange={(event) =>
                    setMaintenanceAcknowledged(event.target.checked)
                  }
                />
                Li a prévia e autorizo explicitamente esta operação.
              </label>
              <div className="admin-actions">
                <button
                  className="button button--danger"
                  type="button"
                  disabled={
                    Boolean(pendingAction) ||
                    !maintenanceAcknowledged ||
                    (maintenancePreview.action === "RESET_ELECTION" &&
                      maintenanceConfirmationPhrase !== "REINICIAR ELEIÇÃO")
                  }
                  onClick={() => void confirmMaintenance()}
                >
                  Confirmar definitivamente
                </button>
                <button
                  className="button button--secondary"
                  type="button"
                  disabled={Boolean(pendingAction)}
                  onClick={() => {
                    setMaintenancePreview(null);
                    setMaintenanceNotice(null);
                    setMaintenanceAcknowledged(false);
                    setMaintenanceConfirmationPhrase("");
                  }}
                >
                  Cancelar
                </button>
              </div>
            </div>
          )}
        </section>
      )}
    </section>
  );
}
