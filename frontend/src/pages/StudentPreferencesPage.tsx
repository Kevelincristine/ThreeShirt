import { useEffect, useState, type FormEvent } from "react";
import { Alert } from "../components/feedback/Alert";
import { Loading } from "../components/feedback/Loading";
import {
  ApiError,
  getStudentDashboard,
  getStudentPreferences,
  saveStudentPreferences,
} from "../services/api";
import type {
  StudentDashboardResponse,
  StudentPreferences,
} from "../types/api";

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

const initialPreferences: StudentPreferences = {
  base_color: "BLACK",
  related_to_course: false,
  wants_animal: false,
  preferred_animal: "",
  preferred_colors: [],
  wants_collar: false,
  wants_phrase: false,
  intends_to_buy: false,
};

type PageState =
  | { status: "loading" }
  | {
      status: "ready";
      dashboard: StudentDashboardResponse;
      preferences: StudentPreferences | null;
    }
  | { status: "error"; message: string };

export function StudentPreferencesPage() {
  const [state, setState] = useState<PageState>({ status: "loading" });
  const [form, setForm] = useState(initialPreferences);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  async function load(signal?: AbortSignal): Promise<void> {
    setState({ status: "loading" });
    try {
      const [dashboard, response] = await Promise.all([
        getStudentDashboard(signal),
        getStudentPreferences(signal),
      ]);
      if (signal?.aborted) return;
      setForm(response.preferences ?? initialPreferences);
      setState({
        status: "ready",
        dashboard,
        preferences: response.preferences,
      });
    } catch (reason) {
      if (!signal?.aborted) {
        setState({
          status: "error",
          message: reason instanceof Error
            ? reason.message
            : "Não foi possível carregar a pesquisa.",
        });
      }
    }
  }

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, []);

  function update<K extends keyof StudentPreferences>(
    field: K,
    value: StudentPreferences[K],
  ): void {
    setForm((current) => ({ ...current, [field]: value }));
  }

  async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setSaving(true);
    setNotice("");
    setError("");
    try {
      const response = await saveStudentPreferences(form);
      setForm(response.preferences ?? initialPreferences);
      setState((current) =>
        current.status === "ready"
          ? {
              ...current,
              preferences: response.preferences,
              dashboard: { ...current.dashboard, has_preferences: true },
            }
          : current,
      );
      setNotice(response.message);
    } catch (reason) {
      setError(
        reason instanceof ApiError
          ? reason.message
          : "Não foi possível salvar as preferências.",
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="student-page" aria-labelledby="preferences-title">
      <header className="paper-panel student-page__header">
        <p className="eyebrow">Pesquisa da turma</p>
        <h1 id="preferences-title">Suas preferências</h1>
        <p>Conte à turma o que você gostaria de ver na camiseta.</p>
      </header>

      {state.status === "loading" && <Loading label="Carregando suas preferências..." />}
      {state.status === "error" && (
        <div className="student-page__panel">
          <Alert variant="error">{state.message}</Alert>
          <button
            className="button button--primary"
            onClick={() => void load()}
            type="button"
          >
            Tentar novamente
          </button>
        </div>
      )}
      {state.status === "ready" && (
        <form className="paper-panel student-form" onSubmit={(event) => void submit(event)}>
          {!state.dashboard.survey_open && (
            <Alert variant="warning">
              A pesquisa está fechada. Suas respostas salvas continuam disponíveis para consulta.
            </Alert>
          )}
          {state.preferences && (
            <Alert variant="info">
              Você já respondeu. Enquanto a pesquisa estiver aberta, pode atualizar e reenviar suas respostas.
            </Alert>
          )}
          {notice && <Alert variant="success">{notice}</Alert>}
          {error && <Alert variant="error">{error}</Alert>}

          <div className="form-field">
            <label htmlFor="base-color">Cor-base preferida</label>
            <select
              id="base-color"
              onChange={(event) => {
                const value = event.target.value;
                if (value === "BLACK" || value === "WHITE") {
                  update("base_color", value);
                }
              }}
              required
              value={form.base_color}
            >
              <option value="BLACK">Preta</option>
              <option value="WHITE">Branca</option>
            </select>
          </div>

          <BooleanField
            label="Você gostaria que a camiseta fosse relacionada ao curso?"
            name="related-to-course"
            onChange={(value) => update("related_to_course", value)}
            value={form.related_to_course}
          />
          <BooleanField
            label="Você gostaria de um tema com animal?"
            name="wants-animal"
            onChange={(value) => {
              update("wants_animal", value);
              if (!value) update("preferred_animal", "");
            }}
            value={form.wants_animal}
          />
          {form.wants_animal && (
            <div className="form-field">
              <label htmlFor="preferred-animal">Qual animal?</label>
              <input
                id="preferred-animal"
                maxLength={60}
                onChange={(event) => update("preferred_animal", event.target.value)}
                required
                value={form.preferred_animal}
              />
            </div>
          )}
          <fieldset className="choice-fieldset">
            <legend>Quais cores adicionais você prefere? (opcional)</legend>
            <div className="choice-grid">
              {colors.map(([value, label]) => (
                <label className="choice-option" key={value}>
                  <input
                    checked={form.preferred_colors.includes(value)}
                    onChange={(event) => {
                      const selected = event.target.checked
                        ? [...form.preferred_colors, value]
                        : form.preferred_colors.filter((color) => color !== value);
                      update("preferred_colors", selected);
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
            label="Você prefere camiseta com gola?"
            name="wants-collar"
            onChange={(value) => update("wants_collar", value)}
            value={form.wants_collar}
          />
          <BooleanField
            label="Você gostaria de uma frase na camiseta?"
            name="wants-phrase"
            onChange={(value) => update("wants_phrase", value)}
            value={form.wants_phrase}
          />
          <BooleanField
            label="Você pretende comprar a camiseta?"
            name="intends-to-buy"
            onChange={(value) => update("intends_to_buy", value)}
            value={form.intends_to_buy}
          />
          <button
            className="button button--primary"
            disabled={!state.dashboard.survey_open || saving}
            type="submit"
          >
            {saving ? "Salvando..." : state.preferences ? "Atualizar respostas" : "Enviar respostas"}
          </button>
        </form>
      )}
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
