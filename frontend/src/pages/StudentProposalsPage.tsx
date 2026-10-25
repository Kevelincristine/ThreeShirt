import { useEffect, useState, type FormEvent } from "react";
import { Alert } from "../components/feedback/Alert";
import { Loading } from "../components/feedback/Loading";
import {
  ApiError,
  getStudentSubmissions,
  submitStudentProposal,
} from "../services/api";
import type { StudentSubmission } from "../types/api";

const colors = [
  ["RED", "Vermelho"],
  ["BLUE", "Azul"],
  ["YELLOW", "Amarelo"],
  ["GREEN", "Verde"],
  ["ORANGE", "Laranja"],
  ["PURPLE", "Roxo/violeta"],
  ["PINK", "Rosa"],
  ["BROWN", "Marrom"],
  ["BLACK", "Preto"],
  ["WHITE", "Branco"],
] as const;

const statusLabels = {
  PENDING: "Aguardando análise",
  APPROVED: "Aprovada",
  REJECTED: "Rejeitada",
} as const;

type SubmissionState =
  | { status: "loading" }
  | { status: "ready"; submissions: StudentSubmission[] }
  | { status: "error"; message: string };

export function StudentProposalsPage() {
  const [state, setState] = useState<SubmissionState>({ status: "loading" });
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [baseColor, setBaseColor] = useState<"BLACK" | "WHITE">("BLACK");
  const [relatedToCourse, setRelatedToCourse] = useState(false);
  const [animal, setAnimal] = useState("");
  const [additionalColors, setAdditionalColors] = useState<string[]>([]);
  const [hasCollar, setHasCollar] = useState(false);
  const [hasPhrase, setHasPhrase] = useState(false);
  const [phraseText, setPhraseText] = useState("");
  const [image, setImage] = useState<File | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  async function load(signal?: AbortSignal): Promise<void> {
    setState({ status: "loading" });
    try {
      const submissions = await getStudentSubmissions(signal);
      if (!signal?.aborted) setState({ status: "ready", submissions });
    } catch (reason) {
      if (!signal?.aborted) {
        setState({
          status: "error",
          message: reason instanceof Error
            ? reason.message
            : "Não foi possível carregar suas propostas.",
        });
      }
    }
  }

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, []);

  async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setNotice("");
    setError("");
    if (!image) {
      setError("Selecione a imagem da proposta.");
      return;
    }
    if (image.size > 5 * 1024 * 1024) {
      setError("A imagem deve ter no máximo 5 MB.");
      return;
    }

    const payload = new FormData();
    payload.set("title", title);
    payload.set("description", description);
    payload.set("image", image);
    payload.set("base_color", baseColor);
    payload.set("related_to_course", String(relatedToCourse));
    payload.set("animal", animal);
    payload.set("additional_colors", JSON.stringify(additionalColors));
    payload.set("has_collar", String(hasCollar));
    payload.set("has_phrase", String(hasPhrase));
    payload.set("phrase_text", phraseText);

    setSubmitting(true);
    try {
      const proposal = await submitStudentProposal(payload);
      setNotice(`“${proposal.title}” foi enviada e aguarda análise.`);
      setTitle("");
      setDescription("");
      setAnimal("");
      setAdditionalColors([]);
      setPhraseText("");
      setHasPhrase(false);
      setImage(null);
      const fileInput = document.getElementById("proposal-image");
      if (fileInput instanceof HTMLInputElement) fileInput.value = "";
      await load();
    } catch (reason) {
      setError(
        reason instanceof ApiError
          ? reason.message
          : "Não foi possível enviar a proposta.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="student-page" aria-labelledby="proposals-title">
      <header className="paper-panel student-page__header">
        <p className="eyebrow">Criação coletiva</p>
        <h1 id="proposals-title">Envie sua proposta</h1>
        <p>As propostas enviadas ficam pendentes até a revisão administrativa.</p>
      </header>

      <form className="paper-panel student-form" onSubmit={(event) => void submit(event)}>
        {notice && <Alert variant="success">{notice}</Alert>}
        {error && <Alert variant="error">{error}</Alert>}

        <div className="form-field">
          <label htmlFor="proposal-title">Título</label>
          <input
            id="proposal-title"
            maxLength={100}
            onChange={(event) => setTitle(event.target.value)}
            required
            value={title}
          />
        </div>
        <div className="form-field">
          <label htmlFor="proposal-description">Descrição</label>
          <textarea
            id="proposal-description"
            maxLength={1000}
            onChange={(event) => setDescription(event.target.value)}
            required
            rows={5}
            value={description}
          />
          <span className="form-hint">{description.length}/1000 caracteres</span>
        </div>
        <div className="form-field">
          <label htmlFor="proposal-image">Imagem do design (PNG, JPEG ou WebP, até 5 MB)</label>
          <input
            accept="image/png,image/jpeg,image/webp"
            id="proposal-image"
            onChange={(event) => setImage(event.target.files?.[0] ?? null)}
            required
            type="file"
          />
        </div>
        <div className="form-field">
          <label htmlFor="proposal-base-color">Cor-base</label>
          <select
            id="proposal-base-color"
            onChange={(event) => {
              const value = event.target.value;
              if (value === "BLACK" || value === "WHITE") {
                setBaseColor(value);
              }
            }}
            value={baseColor}
          >
            <option value="BLACK">Preta</option>
            <option value="WHITE">Branca</option>
          </select>
        </div>
        <BooleanField
          label="Relacionada ao curso"
          name="proposal-course"
          onChange={setRelatedToCourse}
          value={relatedToCourse}
        />
        <div className="form-field">
          <label htmlFor="proposal-animal">Animal (opcional)</label>
          <input
            id="proposal-animal"
            maxLength={60}
            onChange={(event) => setAnimal(event.target.value)}
            value={animal}
          />
        </div>
        <fieldset className="choice-fieldset">
          <legend>Cores adicionais (opcional)</legend>
          <div className="choice-grid">
            {colors.map(([value, label]) => (
              <label className="choice-option" key={value}>
                <input
                  checked={additionalColors.includes(value)}
                  onChange={(event) => {
                    setAdditionalColors((current) =>
                      event.target.checked
                        ? [...current, value]
                        : current.filter((color) => color !== value),
                    );
                  }}
                  type="checkbox"
                  value={value}
                />
                {label}
              </label>
            ))}
          </div>
        </fieldset>
        <BooleanField
          label="Tem gola"
          name="proposal-collar"
          onChange={setHasCollar}
          value={hasCollar}
        />
        <BooleanField
          label="Tem frase"
          name="proposal-has-phrase"
          onChange={(value) => {
            setHasPhrase(value);
            if (!value) setPhraseText("");
          }}
          value={hasPhrase}
        />
        {hasPhrase && (
          <div className="form-field">
            <label htmlFor="proposal-phrase">Frase</label>
            <input
              id="proposal-phrase"
              maxLength={200}
              onChange={(event) => setPhraseText(event.target.value)}
              required
              value={phraseText}
            />
          </div>
        )}
        <button className="button button--primary" disabled={submitting} type="submit">
          {submitting ? "Enviando..." : "Enviar para análise"}
        </button>
      </form>

      <section className="paper-panel student-submissions" aria-labelledby="submission-status-title">
        <h2 id="submission-status-title">Acompanhe suas propostas</h2>
        {state.status === "loading" && <Loading label="Carregando suas propostas..." />}
        {state.status === "error" && (
          <>
            <Alert variant="error">{state.message}</Alert>
            <button className="button button--secondary" onClick={() => void load()} type="button">
              Tentar novamente
            </button>
          </>
        )}
        {state.status === "ready" && state.submissions.length === 0 && (
          <p>Você ainda não enviou propostas.</p>
        )}
        {state.status === "ready" && state.submissions.length > 0 && (
          <ul className="submission-list">
            {state.submissions.map((submission) => (
              <li className="submission-list__item" key={submission.id}>
                <h3>{submission.title}</h3>
                <p>{submission.description}</p>
                <p>
                  <strong>Situação:</strong> {statusLabels[submission.status]}
                </p>
                <time dateTime={submission.created_at}>
                  Enviada em {new Date(submission.created_at).toLocaleDateString("pt-BR")}
                </time>
              </li>
            ))}
          </ul>
        )}
      </section>
    </section>
  );
}

function BooleanField({
  label,
  name,
  value,
  onChange,
}: {
  label: string;
  name: string;
  value: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <div className="form-field">
      <label htmlFor={name}>{label}</label>
      <select
        id={name}
        onChange={(event) => onChange(event.target.value === "true")}
        required
        value={String(value)}
      >
        <option value="true">Sim</option>
        <option value="false">Não</option>
      </select>
    </div>
  );
}
