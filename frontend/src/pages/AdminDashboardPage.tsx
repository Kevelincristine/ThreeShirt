import { useEffect, useMemo, useState } from "react";
import { Alert } from "../components/feedback/Alert";
import { Loading } from "../components/feedback/Loading";
import {
  getAdminDashboard,
  getAdminProposals,
  getAdminStages,
  getAdminUsers,
  reviewAdminProposal,
  updateAdminStage,
  updateAdminUserRole,
  updateAdminUserStatus,
} from "../services/api";
import type { AdminDashboardResponse, AdminProposal, AdminUser } from "../types/api";

function formatPercent(value: number | null): string {
  if (value == null) return "—";
  return `${value.toFixed(1)}%`;
}

export function AdminDashboardPage() {
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

  async function handleStageToggle(stage: "survey" | "proposal_submission" | "voting", action: "open" | "close") {
    if (pendingAction) return;
    const actionLabel = action === "open" ? "abrir" : "encerrar";
    const stageLabel =
      stage === "survey"
        ? "pesquisa"
        : stage === "proposal_submission"
          ? "envio de propostas"
          : "votação";
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

  async function handleUserRole(userId: number, role: "ADMIN" | "STUDENT") {
    if (pendingAction) return;
    const result = window.confirm(
      role === "ADMIN" ? "Conceder acesso administrativo a este usuário?" : "Remover acesso administrativo deste usuário?",
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
                <span>{user.role}</span>
              </div>
              <p>{user.email}</p>
              <div className="admin-actions admin-actions--inline">
                <button
                  className="button button--secondary"
                  type="button"
                  disabled={Boolean(pendingAction)}
                  onClick={() => void handleUserRole(user.id, user.role === "ADMIN" ? "STUDENT" : "ADMIN")}
                >
                  {user.role === "ADMIN" ? "Remover admin" : "Tornar admin"}
                </button>
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
    </section>
  );
}
