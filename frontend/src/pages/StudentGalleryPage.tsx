import { useEffect, useRef, useState } from "react";
import { ConfirmationModal } from "../components/modals/ConfirmationModal";
import { Alert } from "../components/feedback/Alert";
import { Loading } from "../components/feedback/Loading";
import { ShirtCard } from "../components/shirts/ShirtCard";
import { useAuth } from "../app/AuthContext";
import {
  ApiError,
  castStudentVote,
  getApprovedStudentProposals,
  getStudentDashboard,
} from "../services/api";
import type {
  StudentDashboardResponse,
  StudentProposal,
} from "../types/api";

type GalleryState =
  | { status: "loading" }
  | {
      status: "ready";
      proposals: StudentProposal[];
      dashboard: StudentDashboardResponse;
    }
  | { status: "error"; message: string };

export function StudentGalleryPage() {
  const { state: authState } = useAuth();
  const isAdmin =
    authState.status === "authenticated" && authState.user.role === "admin";
  const [state, setState] = useState<GalleryState>({ status: "loading" });
  const [selected, setSelected] = useState<StudentProposal | null>(null);
  const [voting, setVoting] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const voteInProgress = useRef(false);

  async function load(signal?: AbortSignal): Promise<void> {
    setState({ status: "loading" });
    try {
      const [proposals, dashboard] = await Promise.all([
        getApprovedStudentProposals(signal),
        getStudentDashboard(signal),
      ]);
      if (!signal?.aborted) {
        setState({ status: "ready", proposals, dashboard });
      }
    } catch (reason) {
      if (!signal?.aborted) {
        setState({
          status: "error",
          message: reason instanceof Error
            ? reason.message
            : "Não foi possível carregar as camisetas aprovadas.",
        });
      }
    }
  }

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, []);

  async function confirmVote(): Promise<void> {
    if (!selected || voteInProgress.current) return;
    voteInProgress.current = true;
    setVoting(true);
    setError("");
    setNotice("");
    try {
      await castStudentVote(selected.id);
      setState((current) =>
        current.status === "ready"
          ? {
              ...current,
              dashboard: { ...current.dashboard, has_voted: true },
            }
          : current,
      );
      setNotice("Seu voto foi confirmado pelo sistema.");
      setSelected(null);
      try {
        const dashboard = await getStudentDashboard();
        setState((current) =>
          current.status === "ready" ? { ...current, dashboard } : current,
        );
      } catch {
        setError("O voto foi confirmado, mas não foi possível atualizar o painel.");
      }
    } catch (reason) {
      setError(
        reason instanceof ApiError
          ? reason.message
          : "Não foi possível registrar o voto.",
      );
    } finally {
      voteInProgress.current = false;
      setVoting(false);
    }
  }

  return (
    <section className="student-page" aria-labelledby="gallery-title">
      <header className="paper-panel student-page__header">
        <p className="eyebrow">Escolha da camiseta</p>
        <h1 id="gallery-title">Propostas aprovadas</h1>
        <p>Veja os designs aprovados pela equipe para a votação da turma.</p>
      </header>

      {notice && <Alert variant="success">{notice}</Alert>}
      {error && <Alert variant="error">{error}</Alert>}
      {state.status === "loading" && <Loading label="Carregando propostas aprovadas..." />}
      {state.status === "error" && (
        <div className="student-page__panel">
          <Alert variant="error">{state.message}</Alert>
          <button className="button button--primary" onClick={() => void load()} type="button">
            Tentar novamente
          </button>
        </div>
      )}
      {state.status === "ready" && state.proposals.length === 0 && (
        <div className="student-page__panel">
          <Alert variant="info">Ainda não há propostas aprovadas para exibir.</Alert>
        </div>
      )}
      {state.status === "ready" && state.proposals.length > 0 && (
        <>
          {isAdmin && (
            <Alert variant="info">
              Visualização administrativa: a participação na votação é restrita
              a estudantes elegíveis.
            </Alert>
          )}
          {!isAdmin && state.dashboard.voting_open && !state.dashboard.has_voted && (
            <Alert variant="info">
              A votação está aberta. Escolha uma proposta e confirme seu voto.
            </Alert>
          )}
          {!isAdmin && state.dashboard.has_voted && (
            <Alert variant="success">Seu voto já foi registrado.</Alert>
          )}
          {!isAdmin && !state.dashboard.voting_open && !state.dashboard.has_voted && (
            <Alert variant="warning">A votação está fechada no momento.</Alert>
          )}
          <div className="shirt-gallery">
            {state.proposals.map((proposal) => (
              <article className="shirt-gallery__item" key={proposal.id}>
                <ShirtCard
                  description={proposal.description}
                  imageAlt={`Design da proposta ${proposal.title}`}
                  imageUrl={proposal.image_url}
                  title={proposal.title}
                  caption={[
                    proposal.base_color === "BLACK" ? "Base preta" : "Base branca",
                    proposal.related_to_course ? "Relacionada ao curso" : "Tema livre",
                    proposal.animal ? `Animal: ${proposal.animal}` : "",
                    proposal.has_collar ? "Com gola" : "Sem gola",
                    proposal.has_phrase ? `Frase: ${proposal.phrase_text}` : "",
                  ].filter(Boolean).join(" · ")}
                />
                {!isAdmin && state.dashboard.voting_open && !state.dashboard.has_voted && (
                  <button
                    className="button button--primary shirt-gallery__vote"
                    onClick={() => setSelected(proposal)}
                    type="button"
                  >
                    Votar nesta proposta
                  </button>
                )}
              </article>
            ))}
          </div>
        </>
      )}

      <ConfirmationModal
        cancelLabel="Voltar"
        confirmLabel={voting ? "Registrando..." : "Confirmar voto"}
        isOpen={selected !== null}
        message={selected
          ? `Deseja confirmar seu voto em “${selected.title}”? Esta escolha não poderá ser alterada.`
          : ""}
        onCancel={() => {
          if (!voting) setSelected(null);
        }}
        onConfirm={() => void confirmVote()}
        title="Confirmar seu voto"
      />
    </section>
  );
}
