import { useEffect, useState } from "react";
import { Alert } from "../components/feedback/Alert";
import { Loading } from "../components/feedback/Loading";
import { getStudentResults } from "../services/api";
import type { StudentResultsResponse } from "../types/api";

type ResultsState =
  | { status: "loading" }
  | { status: "ready"; results: StudentResultsResponse }
  | { status: "unavailable"; message: string }
  | { status: "error"; message: string };

function formatPercent(value: number | null): string {
  return value === null ? "Dados insuficientes" : `${value.toLocaleString("pt-BR", {
    maximumFractionDigits: 2,
  })}%`;
}

export function StudentResultsPage() {
  const [state, setState] = useState<ResultsState>({ status: "loading" });
  const [retryCount, setRetryCount] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setState({ status: "loading" });
    void getStudentResults(controller.signal).then(
      (results) => {
        if (!controller.signal.aborted) setState({ status: "ready", results });
      },
      (reason: unknown) => {
        if (controller.signal.aborted) return;
        const message = reason instanceof Error
          ? reason.message
          : "Não foi possível consultar os resultados.";
        const status = typeof reason === "object" && reason !== null &&
          "status" in reason && typeof reason.status === "number"
          ? reason.status
          : 0;
        setState(
          status === 403 || status === 409
            ? { status: "unavailable", message }
            : { status: "error", message },
        );
      },
    );
    return () => controller.abort();
  }, [retryCount]);

  return (
    <section className="student-page" aria-labelledby="results-title">
      <header className="paper-panel student-page__header">
        <p className="eyebrow">Apuração da turma</p>
        <h1 id="results-title">Resultados da eleição</h1>
        <p>Os dados são agregados e aparecem somente após autorização de divulgação e encerramento da votação.</p>
      </header>

      {state.status === "loading" && <Loading label="Consultando resultados autorizados..." />}
      {state.status === "unavailable" && (
        <div className="student-page__panel">
          <Alert variant="warning">{state.message}</Alert>
        </div>
      )}
      {state.status === "error" && (
        <div className="student-page__panel">
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
      {state.status === "ready" && (
        <>
          {state.results.status === "no_votes" && (
            <Alert variant="info">A divulgação foi autorizada, mas ainda não há votos registrados.</Alert>
          )}
          {state.results.status === "complete" && (
            <>
              <section className="paper-panel results-panel" aria-labelledby="vote-results-title">
                <h2 id="vote-results-title">Votação</h2>
                <p>Total de votos válidos: <strong>{state.results.total_votes}</strong></p>
                {state.results.is_tie ? (
                  <Alert variant="info">
                    Empate entre: {state.results.winners.map((winner) => winner.title).join(", ")}.
                  </Alert>
                ) : (
                  <p className="results-winner">
                    Mais votada: <strong>{state.results.winners[0]?.title ?? "—"}</strong>
                  </p>
                )}
                <ul className="results-list">
                  {state.results.proposals.map((proposal) => (
                    <li className="results-list__item" key={proposal.proposal_id}>
                      <div className="results-list__heading">
                        <strong>{proposal.title}</strong>
                        <span>{proposal.vote_count} votos · {formatPercent(proposal.percentage)}</span>
                      </div>
                      <div
                        aria-label={`Percentual de votos de ${proposal.title}`}
                        aria-valuemax={100}
                        aria-valuemin={0}
                        aria-valuenow={proposal.percentage ?? 0}
                        className="results-bar"
                        role="meter"
                      >
                        <span style={{ width: `${proposal.percentage ?? 0}%` }} />
                      </div>
                    </li>
                  ))}
                </ul>
              </section>
            </>
          )}

          <section className="paper-panel results-panel" aria-labelledby="compatibility-title">
            <h2 id="compatibility-title">Compatibilidade média com as preferências</h2>
            {state.results.compatibility_status === "insufficient_data" ? (
              <p>Ainda não há respostas suficientes para calcular compatibilidade.</p>
            ) : (
              <>
                {state.results.compatibility_is_tie ? (
                  <Alert variant="info">
                    Maior compatibilidade média: {state.results.compatibility_winners
                      .map((winner) => winner.title).join(", ")}.
                  </Alert>
                ) : (
                  <p className="results-winner">
                    Maior compatibilidade média:{" "}
                    <strong>{state.results.compatibility_winners[0]?.title ?? "—"}</strong>
                  </p>
                )}
                <ul className="results-list">
                  {state.results.proposals.map((proposal) => (
                    <li className="results-list__item" key={proposal.proposal_id}>
                      <div className="results-list__heading">
                        <strong>{proposal.title}</strong>
                        <span>
                          {formatPercent(proposal.compatibility)} · {proposal.valid_assessments} avaliações válidas
                        </span>
                      </div>
                    </li>
                  ))}
                </ul>
              </>
            )}
          </section>
        </>
      )}
    </section>
  );
}
