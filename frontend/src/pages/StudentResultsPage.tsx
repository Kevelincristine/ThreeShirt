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
  const voteWinnerIds = new Set(
    state.status === "ready"
      ? state.results.winners.map((winner) => winner.proposal_id)
      : [],
  );
  const compatibilityWinnerIds = new Set(
    state.status === "ready"
      ? state.results.compatibility_winners
          .filter((winner) => winner.status === "available")
          .map((winner) => winner.proposal_id)
      : [],
  );

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
                <h2 id="vote-results-title">Resultado da votação</h2>
                <div className="results-total">
                  <span>Total de votos válidos</span>
                  <strong>{state.results.total_votes}</strong>
                </div>
                {state.results.is_tie && (
                  <Alert variant="info">
                    Empate entre: {state.results.winners.map((winner) => winner.title).join(", ")}.
                  </Alert>
                )}
                {state.results.winners.length > 0 ? (
                  <div className="results-winner-grid">
                    {state.results.winners.map((winner) => {
                      const proposal = state.results.status === "complete"
                        ? state.results.proposals.find(
                            (item) => item.proposal_id === winner.proposal_id,
                          )
                        : undefined;
                      return (
                        <article className="results-winner-card" key={winner.proposal_id}>
                          {proposal && (
                            <img
                              alt={`Fotografia da proposta ${winner.title}`}
                              className="results-winner-card__image"
                              src={proposal.image_url}
                            />
                          )}
                          <div className="results-winner-card__details">
                            <span className="results-badge">Proposta vencedora</span>
                            <h3>{winner.title}</h3>
                            <p>
                              <strong>{winner.vote_count}</strong>{" "}
                              {winner.vote_count === 1 ? "voto" : "votos"}
                            </p>
                            <p>
                              <strong>{formatPercent(winner.percentage)}</strong> dos votos válidos
                            </p>
                          </div>
                        </article>
                      );
                    })}
                  </div>
                ) : (
                  <Alert variant="info">
                    A apuração não identificou uma proposta vencedora.
                  </Alert>
                )}
                <ul className="results-list">
                  {state.results.proposals.map((proposal) => (
                    <li
                      className={`results-list__item ${
                        voteWinnerIds.has(proposal.proposal_id)
                          ? "results-list__item--winner"
                          : ""
                      }`}
                      key={proposal.proposal_id}
                    >
                      <div className="results-list__heading">
                        <strong>{proposal.title}</strong>
                        <span>{proposal.vote_count} votos · {formatPercent(proposal.percentage)}</span>
                      </div>
                      {voteWinnerIds.has(proposal.proposal_id) && (
                        <span className="results-badge">Mais votada</span>
                      )}
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

          <section className="paper-panel results-panel survey-results-panel" aria-labelledby="survey-results-title">
            <header className="survey-results__header">
              <h2 id="survey-results-title">Resultado da pesquisa</h2>
              <p>
                {state.results.survey_results.respondent_count}{" "}
                {state.results.survey_results.respondent_count === 1
                  ? "resposta"
                  : "respostas"}
              </p>
            </header>
            {state.results.survey_results.questions.length === 0 ? (
              <Alert variant="info">Ainda não há perguntas da pesquisa disponíveis.</Alert>
            ) : (
              <div className="survey-results__questions">
                {state.results.survey_results.questions.map((question) => (
                  <article className="survey-question" key={question.id}>
                    <header className="survey-question__header">
                      <h3>{question.question}</h3>
                      <span>
                        {question.respondent_count}{" "}
                        {question.respondent_count === 1 ? "resposta" : "respostas"}
                      </span>
                    </header>
                    {question.id === "preferred_colors" && (
                      <p className="survey-question__note">
                        Cada estudante podia escolher mais de uma cor; os percentuais mostram a parcela de respostas que selecionou cada opção.
                      </p>
                    )}
                    {question.options.length === 0 ? (
                      <p className="survey-question__empty">
                        Nenhuma resposta registrada para esta pergunta.
                      </p>
                    ) : (
                      <ul className="survey-question__options">
                        {question.options.map((option) => (
                          <li className="survey-option" key={option.value}>
                            <div className="survey-option__heading">
                              <strong>{option.label}</strong>
                              <span>
                                {option.response_count}{" "}
                                {option.response_count === 1 ? "resposta" : "respostas"}
                                {" · "}
                                {option.percentage === null
                                  ? "Sem respostas"
                                  : formatPercent(option.percentage)}
                              </span>
                            </div>
                            <div
                              aria-label={`${option.label}: ${option.percentage ?? 0}%`}
                              aria-valuemax={100}
                              aria-valuemin={0}
                              aria-valuenow={option.percentage ?? 0}
                              className="results-bar"
                              role="meter"
                            >
                              <span
                                style={{
                                  width: `${Math.min(100, Math.max(0, option.percentage ?? 0))}%`,
                                }}
                              />
                            </div>
                          </li>
                        ))}
                      </ul>
                    )}
                  </article>
                ))}
              </div>
            )}
          </section>

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
                    <li
                      className={`results-list__item ${
                        compatibilityWinnerIds.has(proposal.proposal_id)
                          ? "results-list__item--winner"
                          : ""
                      }`}
                      key={proposal.proposal_id}
                    >
                      <div className="results-list__heading">
                        <strong>{proposal.title}</strong>
                        <span>
                          {formatPercent(proposal.compatibility)} · {proposal.valid_assessments} avaliações válidas
                        </span>
                      </div>
                      {compatibilityWinnerIds.has(proposal.proposal_id) && (
                        <span className="results-badge">Maior compatibilidade</span>
                      )}
                      {proposal.compatibility !== null && (
                        <div className="results-compatibility-meter">
                          <div
                            aria-label={`Compatibilidade de ${proposal.title}`}
                            aria-valuemax={100}
                            aria-valuemin={0}
                            aria-valuenow={proposal.compatibility}
                            className="results-bar"
                            role="meter"
                          >
                            <span
                              style={{ width: `${proposal.compatibility}%` }}
                            />
                          </div>
                        </div>
                      )}
                    </li>
                  ))}
                </ul>
              </>
            )}
          </section>

          <section
            className="paper-panel results-panel"
            aria-labelledby="compatibility-winner-title"
          >
            <h2 id="compatibility-winner-title">
              Camisa com maior compatibilidade
            </h2>
            {state.results.compatibility_status === "insufficient_data" ? (
              <p>
                Ainda não há respostas suficientes para identificar a camisa que mais combinou com as preferências da turma.
              </p>
            ) : state.results.compatibility_winners.length === 0 ? (
              <p>
                Não foi possível identificar uma camisa com maior compatibilidade.
              </p>
            ) : (
              <div className="results-winner-grid">
                {state.results.compatibility_winners.map((winner) => {
                  const proposal = state.results.proposals.find(
                    (item) => item.proposal_id === winner.proposal_id,
                  );
                  if (!proposal || winner.average_compatibility === null) {
                    return null;
                  }
                  return (
                    <article
                      className="results-winner-card"
                      key={winner.proposal_id}
                    >
                      <img
                        alt={`Fotografia da proposta ${winner.title}`}
                        className="results-winner-card__image"
                        src={proposal.image_url}
                      />
                      <div className="results-winner-card__details">
                        <span className="results-badge">
                          Mais compatível com as preferências
                        </span>
                        <h3>{winner.title}</h3>
                        <p>
                          Compatibilidade média:{" "}
                          <strong>
                            {formatPercent(winner.average_compatibility)}
                          </strong>
                        </p>
                      </div>
                    </article>
                  );
                })}
              </div>
            )}
          </section>
        </>
      )}
    </section>
  );
}
