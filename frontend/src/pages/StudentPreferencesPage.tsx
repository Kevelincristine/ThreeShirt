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

type SurveyForm = Omit<
  StudentPreferences,
  | "base_color"
  | "related_to_course"
  | "wants_animal"
  | "wants_collar"
  | "wants_phrase"
  | "intends_to_buy"
> & {
  base_color: StudentPreferences["base_color"] | "";
  related_to_course: boolean | null;
  wants_animal: boolean | null;
  wants_collar: boolean | null;
  wants_phrase: boolean | null;
  intends_to_buy: boolean | null;
};

const initialPreferences: SurveyForm = {
  base_color: "",
  related_to_course: null,
  wants_animal: null,
  preferred_animal: "",
  preferred_colors: [],
  wants_collar: null,
  wants_phrase: null,
  intends_to_buy: null,
};

function getPreferencesPayload(form: SurveyForm): StudentPreferences | null {
  const {
    base_color,
    related_to_course,
    wants_animal,
    wants_collar,
    wants_phrase,
    intends_to_buy,
  } = form;
  if (
    !base_color ||
    related_to_course === null ||
    wants_animal === null ||
    wants_collar === null ||
    wants_phrase === null ||
    intends_to_buy === null ||
    (wants_animal && !form.preferred_animal.trim())
  ) {
    return null;
  }

  return {
    ...form,
    base_color,
    related_to_course,
    wants_animal,
    wants_collar,
    wants_phrase,
    intends_to_buy,
    preferred_animal: wants_animal
      ? form.preferred_animal.trim()
      : "",
  };
}

function preferencesMatch(
  form: SurveyForm,
  preferences: StudentPreferences | null,
): boolean {
  if (!preferences) return false;
  const payload = getPreferencesPayload(form);
  return Boolean(
    payload &&
      payload.base_color === preferences.base_color &&
      payload.related_to_course === preferences.related_to_course &&
      payload.wants_animal === preferences.wants_animal &&
      payload.preferred_animal === preferences.preferred_animal &&
      payload.preferred_colors.length === preferences.preferred_colors.length &&
      payload.preferred_colors.every((color) =>
        preferences.preferred_colors.includes(color),
      ) &&
      payload.wants_collar === preferences.wants_collar &&
      payload.wants_phrase === preferences.wants_phrase &&
      payload.intends_to_buy === preferences.intends_to_buy,
  );
}

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
  const [form, setForm] = useState<SurveyForm>(initialPreferences);
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
    value: SurveyForm[K],
  ): void {
    setForm((current) => ({ ...current, [field]: value }));
    setNotice("");
    setError("");
  }

  async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const payload = getPreferencesPayload(form);
    if (
      state.status !== "ready" ||
      !state.dashboard.survey_open ||
      !payload
    ) {
      return;
    }
    setSaving(true);
    setNotice("");
    setError("");
    try {
      const response = await saveStudentPreferences(payload);
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
          : "Não foi possível salvar as respostas.",
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="student-page" aria-labelledby="preferences-title">
      <header className="paper-panel student-page__header">
        <p className="eyebrow">Pesquisa da turma</p>
        <h1 id="preferences-title">Pesquisa</h1>
        <p>Conte à turma o que você gostaria de ver na camiseta.</p>
      </header>

      {state.status === "loading" && <Loading label="Carregando pesquisa..." />}
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
          <fieldset
            className="survey-fields"
            disabled={!state.dashboard.survey_open || saving}
          >
            <legend>Suas respostas</legend>
            <div className="form-field">
              <label htmlFor="base-color">Cor-base preferida</label>
              <select
                id="base-color"
                onChange={(event) => {
                  const value = event.target.value;
                  if (value === "" || value === "BLACK" || value === "WHITE") {
                    update("base_color", value);
                  }
                }}
                required
                value={form.base_color}
              >
                <option disabled value="">Selecione uma opção</option>
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
            {form.wants_animal === true && (
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
          </fieldset>

          <div
            aria-live="polite"
            className={`survey-status ${
              !state.dashboard.survey_open
                ? "survey-status--closed"
                : preferencesMatch(form, state.preferences)
                  ? "survey-status--submitted"
                  : getPreferencesPayload(form)
                    ? "survey-status--ready"
                    : "survey-status--incomplete"
            }`}
          >
            <strong>
              {!state.dashboard.survey_open
                ? "Pesquisa fechada"
                : preferencesMatch(form, state.preferences)
                  ? "Pesquisa já enviada"
                  : getPreferencesPayload(form)
                    ? "Respostas prontas para envio"
                    : "Pesquisa incompleta"}
            </strong>
            <span>
              {!state.dashboard.survey_open
                ? "Não é possível enviar ou atualizar respostas agora."
                : preferencesMatch(form, state.preferences)
                  ? "O envio foi confirmado pelo sistema. Você pode atualizar suas respostas enquanto a pesquisa estiver aberta."
                  : getPreferencesPayload(form)
                    ? "Revise suas respostas e envie para registrar a pesquisa."
                    : "Preencha as perguntas obrigatórias para liberar o envio."}
            </span>
          </div>
          {notice && <Alert variant="success">{notice}</Alert>}
          {error && <Alert variant="error">{error}</Alert>}
          <button
            className={`button button--primary survey-submit ${
              !state.dashboard.survey_open
                ? "survey-submit--closed"
                : getPreferencesPayload(form)
                  ? "survey-submit--ready"
                  : "survey-submit--incomplete"
            }`}
            disabled={
              !state.dashboard.survey_open ||
              saving ||
              !getPreferencesPayload(form)
            }
            type="submit"
          >
            {!state.dashboard.survey_open
              ? "Pesquisa fechada"
              : saving
                ? "Enviando..."
                : state.preferences
                  ? "Atualizar respostas"
                  : getPreferencesPayload(form)
                    ? "Enviar respostas"
                    : "Responda às perguntas obrigatórias"}
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
  value: boolean | null;
  onChange: (value: boolean | null) => void;
}) {
  return (
    <div className="form-field">
      <label htmlFor={name}>{label}</label>
      <select
        id={name}
        onChange={(event) =>
          onChange(
            event.target.value === ""
              ? null
              : event.target.value === "true",
          )
        }
        required
        value={value === null ? "" : String(value)}
      >
        <option disabled value="">Selecione uma opção</option>
        <option value="true">Sim</option>
        <option value="false">Não</option>
      </select>
    </div>
  );
}
