import { useEffect, useMemo, useState } from "react";
import { Alert } from "../components/feedback/Alert";
import { Loading } from "../components/feedback/Loading";
import {
  getAdminDashboard,
  getAdminProposals,
  getAdminStages,
  getAdminUsers,
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
} from "../types/api";
import { useAuth } from "../app/AuthContext";

const roleLabels: Record<AdminUser["role"], string> = {
  STUDENT: "Estudante",
  STUDENT_ADMIN: "Estudante e administrador",
  ADMIN: "Administrador",
};

const maintenanceCountLabels: Record<string, string> = {
  accounts: "Contas excluídas",
  profiles: "Perfis excluídos",
  preference_responses: "Respostas de pesquisa excluídas",
  proposals: "Propostas excluídas",
  votes: "Votos excluídos",
  email_login_codes: "Códigos de acesso removidos",
  group_memberships: "Vínculos com grupos removidos",
  direct_permissions: "Permissões diretas removidas",
  admin_log_entries: "Registros administrativos removidos",
  proposal_images: "Imagens de proposta afetadas",
  files_to_remove: "Arquivos físicos a remover",
  votes_from_other_accounts: "Votos de outras contas afetados",
  settings_rows: "Configurações eleitorais restauradas",
  accounts_preserved: "Contas preservadas",
  profiles_preserved: "Perfis preservados",
  email_login_codes_preserved: "Códigos de acesso preservados",
  login_rate_buckets_preserved: "Controles de limite de login preservados",
};

const maintenanceActionLabels: Record<AdminMaintenancePreview["action"], string> = {
  DELETE_USER: "Exclusão de conta",
  DELETE_PROPOSAL: "Exclusão de proposta",
  RESET_ELECTION: "Reinicialização eleitoral",
};

const proposalStatusLabels = {
  PENDING: "Aguardando aprovação",
  APPROVED: "Aprovada",
  REJECTED: "Rejeitada",
};

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
  const [maintenancePhrase, setMaintenancePhrase] = useState("");
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
  }, []);

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
    const result = window.confirm(
      `Alterar o papel deste usuário para ${roleLabels[role]}?`,
    );
    if (!result) return;

    setPendingAction(`role:${userId}:${role}`);
    try {
      await updateAdminUserRole(userId, role);
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
    const result = window.confirm(
      isActive ? "Ativar esta conta?" : "Desativar esta conta?",
    );
    if (!result) return;

    setPendingAction(`status:${userId}:${String(isActive)}`);
    try {
      await updateAdminUserStatus(userId, isActive);
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
    const operationLabel =
      action === "delete_user"
        ? "a exclusão definitiva desta conta"
        : action === "delete_proposal"
          ? "a exclusão desta proposta e dos votos associados"
          : "a reinicialização de todos os dados eleitorais";
    if (
      !window.confirm(
        `Iniciar ${operationLabel}? A próxima etapa exibirá os dados afetados antes da confirmação final.`,
      )
    ) {
      return;
    }

    setPendingAction(`maintenance:preview:${action}`);
    setMaintenancePreview(null);
    setMaintenancePhrase("");
    setMaintenanceNotice(null);
    try {
      const preview = await previewAdminMaintenance(action, targetId);
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
    if (
      maintenancePreview.action === "RESET_ELECTION" &&
      maintenancePhrase !== "REINICIAR ELEIÇÃO"
    ) {
      setMaintenanceNotice({
        variant: "error",
        message: "Digite exatamente REINICIAR ELEIÇÃO para continuar.",
      });
      return;
    }

    setPendingAction("maintenance:confirm");
    setMaintenanceNotice(null);
    try {
      const result = await confirmAdminMaintenance(
        maintenancePreview.challenge_token,
        maintenancePhrase,
      );
      const notices = [result.message, result.warning].filter(Boolean);
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
      setMaintenancePhrase("");
      await loadData();
    } catch (error) {
      setMaintenancePreview(null);
      setMaintenancePhrase("");
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
              <div className="admin-actions admin-actions--inline">
                {(
                  ["STUDENT", "STUDENT_ADMIN", "ADMIN"] as const
                ).filter((role) => role !== user.role).map((role) => (
                  <button
                    className="button button--secondary"
                    key={role}
                    type="button"
                    disabled={Boolean(pendingAction)}
                    onClick={() => void handleUserRole(user.id, role)}
                  >
                    {`Definir: ${roleLabels[role]}`}
                  </button>
                ))}
                <button
                  className="button button--secondary"
                  type="button"
                  disabled={Boolean(pendingAction)}
                  onClick={() => void handleUserStatus(user.id, !user.is_active)}
                >
                  {user.is_active ? "Desativar" : "Ativar"}
                </button>
              </div>
            </article>
          ))}
        </div>
      </div>

      {canMaintain && (
        <section
          aria-labelledby="maintenance-zone-title"
          className="admin-section maintenance-zone"
        >
          <div className="maintenance-zone__header">
            <p className="eyebrow">Ações irreversíveis</p>
            <h2 id="maintenance-zone-title">Zona de Manutenção</h2>
            <p>
              Faça um backup antes de prosseguir. As prévias são calculadas no
              servidor e expiram após 10 minutos.
            </p>
          </div>

          {maintenanceNotice && (
            <Alert variant={maintenanceNotice.variant}>
              {maintenanceNotice.message}
            </Alert>
          )}

          <div className="maintenance-grid">
            <article className="maintenance-card">
              <h3>Excluir uma conta</h3>
              <p>
                Remove o perfil, respostas e votos da conta, além das propostas
                enviadas e dos votos nelas. Nenhuma outra conta será excluída.
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
                      disabled={user.id === currentUserId}
                    >
                      {user.full_name || user.email} — {roleLabels[user.role]}
                      {user.id === currentUserId ? " (sua conta)" : ""}
                    </option>
                  ))}
                </select>
              </label>
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
            </article>

            <article className="maintenance-card">
              <h3>Excluir uma proposta</h3>
              <p>
                Pode afetar votos e resultados. Propostas pendentes, aprovadas
                e rejeitadas podem ser selecionadas.
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

            <article className="maintenance-card maintenance-card--irreversible">
              <h3>Reiniciar eleição</h3>
              <p>
                Remove respostas, propostas, votos e resultados calculados.
                Todas as contas, perfis, papéis, códigos de acesso e limites de
                login serão preservados.
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
          </div>

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
                  Para confirmar, digite <strong>REINICIAR ELEIÇÃO</strong>
                  <input
                    autoComplete="off"
                    id="maintenance-confirmation-phrase"
                    onChange={(event) => setMaintenancePhrase(event.target.value)}
                    value={maintenancePhrase}
                  />
                </label>
              )}
              <div className="admin-actions">
                <button
                  className="button button--danger"
                  type="button"
                  disabled={
                    Boolean(pendingAction) ||
                    (maintenancePreview.action === "RESET_ELECTION" &&
                      maintenancePhrase !== "REINICIAR ELEIÇÃO")
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
                    setMaintenancePhrase("");
                    setMaintenanceNotice(null);
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
