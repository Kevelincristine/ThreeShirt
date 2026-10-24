import { useEffect, useState } from "react";
import { Alert } from "../components/feedback/Alert";
import { Loading } from "../components/feedback/Loading";
import { getStudentDashboard } from "../services/api";
import type { StudentDashboardResponse } from "../types/api";

type DashboardState =
  | { status: "loading" }
  | { status: "loaded"; dashboard: StudentDashboardResponse }
  | { status: "error"; message: string };

function getErrorMessage(reason: unknown): string {
  if (reason instanceof Error) return reason.message;
  return "Não foi possível carregar o painel. Tente novamente.";
}

function DashboardCard({
  title,
  value,
  description,
}: {
  title: string;
  value: string;
  description: string;
}) {
  return (
    <div className="student-dashboard__card">
      <dt>{title}</dt>
      <dd>{value}</dd>
      <dd className="student-dashboard__description">{description}</dd>
    </div>
  );
}

export function StudentDashboardPage() {
  const [state, setState] = useState<DashboardState>({ status: "loading" });
  const [retryCount, setRetryCount] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setState({ status: "loading" });

    void getStudentDashboard(controller.signal).then(
      (dashboard) => {
        if (!controller.signal.aborted) {
          setState({ status: "loaded", dashboard });
        }
      },
      (reason: unknown) => {
        if (!controller.signal.aborted) {
          setState({ status: "error", message: getErrorMessage(reason) });
        }
      },
    );

    return () => controller.abort();
  }, [retryCount]);

  return (
    <section
      aria-labelledby="student-dashboard-title"
      className="student-dashboard"
    >
      <header className="paper-panel student-dashboard__intro">
        <p className="eyebrow">Caderno da turma</p>
        <h1 id="student-dashboard-title">Painel do estudante</h1>
        <p>
          Acompanhe por aqui as informações atuais da pesquisa, da votação e
          da sua participação.
        </p>
      </header>

      {state.status === "loading" && (
        <div className="student-dashboard__feedback">
          <Loading label="Carregando as informações do painel..." />
        </div>
      )}

      {state.status === "error" && (
        <div className="student-dashboard__feedback">
          <Alert variant="error">{state.message}</Alert>
          <button
            className="button button--primary"
            onClick={() => setRetryCount((count) => count + 1)}
            type="button"
          >
            Tentar novamente
          </button>
        </div>
      )}

      {state.status === "loaded" && (
        <>
          <dl
            aria-label="Estado atual do painel"
            className="student-dashboard__grid"
          >
            <DashboardCard
              description="Estado informado pelo sistema."
              title="Pesquisa de preferências"
              value={state.dashboard.survey_open ? "Aberta" : "Fechada"}
            />
            <DashboardCard
              description="Estado informado pelo sistema."
              title="Votação"
              value={state.dashboard.voting_open ? "Aberta" : "Fechada"}
            />
            <DashboardCard
              description="Esta página não apresenta resultados da eleição."
              title="Divulgação dos resultados"
              value={
                state.dashboard.results_release_authorized
                  ? "Autorizada"
                  : "Ainda não autorizada"
              }
            />
            <DashboardCard
              description="Consulta referente à sua conta."
              title="Suas preferências"
              value={
                state.dashboard.has_preferences
                  ? "Resposta registrada"
                  : "Sem resposta registrada"
              }
            />
            <DashboardCard
              description="Consulta referente à sua conta."
              title="Seu voto"
              value={
                state.dashboard.has_voted
                  ? "Voto registrado"
                  : "Sem voto registrado"
              }
            />
          </dl>
          <Alert variant="info">
            Este painel é informativo. As ações de preferências e votação ainda
            não estão disponíveis nesta interface.
          </Alert>
        </>
      )}
    </section>
  );
}
