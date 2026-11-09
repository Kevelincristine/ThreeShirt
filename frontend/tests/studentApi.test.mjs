import assert from "node:assert/strict";
import test from "node:test";

globalThis.document = {
  cookie: "csrftoken=test-token",
  querySelector: () => null,
};
globalThis.window = Object.assign(new EventTarget(), {
  location: { origin: "http://localhost:5173" },
});

const api = await import("../src/services/api.ts");

function mockResponse(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function setFetch(handler) {
  const original = globalThis.fetch;
  globalThis.fetch = handler;
  return () => {
    globalThis.fetch = original;
  };
}

test("preferences use the authenticated endpoint and submit CSRF-protected JSON", async () => {
  const calls = [];
  const restore = setFetch(async (url, init) => {
    calls.push({ url: String(url), init });
    return mockResponse({
      message: "Preferências salvas.",
      has_preferences: true,
      preferences: {
        base_color: "BLACK",
        related_to_course: true,
        wants_animal: false,
        preferred_animal: "",
        preferred_colors: ["BLUE"],
        wants_collar: true,
        wants_phrase: false,
        intends_to_buy: true,
      },
    });
  });
  try {
    const saved = await api.saveStudentPreferences({
      base_color: "BLACK",
      related_to_course: true,
      wants_animal: false,
      preferred_animal: "",
      preferred_colors: ["BLUE"],
      wants_collar: true,
      wants_phrase: false,
      intends_to_buy: true,
    });
    assert.equal(saved.message, "Preferências salvas.");
    assert.equal(calls[0].url, "http://localhost:5173/api/election/student/preferences/");
    assert.equal(calls[0].init.credentials, "include");
    assert.equal(calls[0].init.headers.get("X-CSRFToken"), "test-token");
    assert.equal(calls[0].init.headers.get("Content-Type"), "application/json");
  } finally {
    restore();
  }
});

test("login, first access, and own-password change use CSRF-protected endpoints", async () => {
  const calls = [];
  const restore = setFetch(async (url, init) => {
    calls.push({ path: new URL(String(url)).pathname, init });
    if (new URL(String(url)).pathname.endsWith("/csrf/")) {
      return mockResponse({ csrfToken: "server-csrf-token" });
    }
    return mockResponse({ message: "Solicitação registrada." });
  });
  try {
    await api.loginWithPassword("student@gmail.com", "Student-password-42!");
    await api.setFirstAccessPassword(
      "student@gmail.com",
      "Replacement-password-73!",
      "Replacement-password-73!",
    );
    await api.changePassword(
      "Student-password-42!",
      "Replacement-password-73!",
      "Replacement-password-73!",
    );

    const posts = calls.filter((call) => call.init.method === "POST");
    assert.deepEqual(posts.map((call) => call.path), [
      "/api/accounts/login/",
      "/api/accounts/first-access/",
      "/api/accounts/password/change/",
    ]);
    assert.equal(posts[0].init.headers.get("X-CSRFToken"), "server-csrf-token");
    assert.deepEqual(JSON.parse(posts[0].init.body), {
      email: "student@gmail.com",
      password: "Student-password-42!",
    });
    assert.deepEqual(JSON.parse(posts[1].init.body), {
      email: "student@gmail.com",
      password: "Replacement-password-73!",
      password_confirmation: "Replacement-password-73!",
    });
    assert.deepEqual(JSON.parse(posts[2].init.body), {
      current_password: "Student-password-42!",
      new_password: "Replacement-password-73!",
      password_confirmation: "Replacement-password-73!",
    });
  } finally {
    restore();
  }
});

test("proposal submission sends multipart form data with CSRF and approved list is read separately", async () => {
  const calls = [];
  const restore = setFetch(async (url, init) => {
    calls.push({ url: String(url), init });
    if (String(url).endsWith("/submissions/")) {
      return mockResponse({
        message: "Proposta enviada.",
        proposal: {
          id: 8,
          title: "Minha camiseta",
          description: "Uma descrição.",
          status: "PENDING",
        },
      }, 201);
    }
    return mockResponse({
      proposals: [{
        id: 3,
        author_id: 44,
        title: "Aprovada",
        description: "Design autorizado.",
        image_url: "/proposals/3/image/",
        base_color: "WHITE",
        related_to_course: true,
        animal: "",
        additional_colors: ["BLUE"],
        has_collar: false,
        has_phrase: false,
        phrase_text: "",
      }],
    });
  });
  try {
    const form = new FormData();
    form.set("title", "Minha camiseta");
    const submitted = await api.submitStudentProposal(form);
    const approved = await api.getApprovedStudentProposals();
    assert.equal(submitted.status, "PENDING");
    assert.equal(calls[0].init.body, form);
    assert.equal(calls[0].init.headers.get("X-CSRFToken"), "test-token");
    assert.equal(calls[0].init.headers.has("Content-Type"), false);
    assert.equal(approved.length, 1);
    assert.equal(approved[0].title, "Aprovada");
    assert.equal(approved[0].author_id, 44);
    assert.equal(calls[1].url, "http://localhost:5173/api/election/student/proposals/");
  } finally {
    restore();
  }
});

test("admin proposal parser preserves nullable survey answers and multiple-choice values", async () => {
  const restore = setFetch(async () => mockResponse({
    proposals: [{
      id: 12,
      title: "Proposta em revisão",
      description: "Descrição do design.",
      status: "PENDING",
      base_color: "BLACK",
      animal: "",
      additional_colors: [],
      has_collar: false,
      has_phrase: false,
      phrase_text: "",
      created_at: "2026-11-02T10:00:00-03:00",
      updated_at: "2026-11-02T10:00:00-03:00",
      reviewed_at: null,
      rejection_reason: "",
      image_url: "/proposals/12/image/",
      preferences: {
        base_color: "WHITE",
        related_to_course: true,
        wants_animal: true,
        preferred_animal: "Raposa",
        preferred_colors: ["BLUE", "RED"],
        wants_collar: false,
        wants_phrase: true,
        intends_to_buy: false,
      },
      author: { id: 9, email: "author@school.test", full_name: "Autor" },
      reviewed_by: null,
    }, {
      id: 13,
      title: "Sem respostas",
      description: "Outra descrição.",
      status: "PENDING",
      base_color: "WHITE",
      animal: "",
      additional_colors: [],
      has_collar: false,
      has_phrase: false,
      phrase_text: "",
      created_at: "2026-11-02T10:00:00-03:00",
      updated_at: "2026-11-02T10:00:00-03:00",
      reviewed_at: null,
      rejection_reason: "",
      image_url: "/proposals/13/image/",
      preferences: null,
      author: { id: 10, email: "other@school.test", full_name: "Outra pessoa" },
      reviewed_by: null,
    }],
  }));
  try {
    const proposals = await api.getAdminProposals();
    assert.deepEqual(proposals[0].preferences, {
      base_color: "WHITE",
      related_to_course: true,
      wants_animal: true,
      preferred_animal: "Raposa",
      preferred_colors: ["BLUE", "RED"],
      wants_collar: false,
      wants_phrase: true,
      intends_to_buy: false,
    });
    assert.equal(proposals[1].preferences, null);
  } finally {
    restore();
  }
});

test("student voting only succeeds when backend confirms the vote", async () => {
  let payload;
  const restore = setFetch(async (_url, init) => {
    payload = JSON.parse(init.body);
    return mockResponse({ message: "Voto registrado.", has_voted: true });
  });
  try {
    await api.castStudentVote(14);
    assert.deepEqual(payload, { proposal_id: 14 });
  } finally {
    restore();
  }

  const restoreRejected = setFetch(async () =>
    mockResponse({ error: "Votação fechada." }, 400),
  );
  try {
    await assert.rejects(
      api.castStudentVote(14),
      (error) => error instanceof api.ApiError && error.status === 400,
    );
  } finally {
    restoreRejected();
  }
});

test("student results parse decimal strings as aggregates without individual records", async () => {
  const restore = setFetch(async () => mockResponse({
    status: "complete",
    total_votes: 2,
    proposals: [{
      proposal_id: 4,
      title: "Design finalista",
      image_url: "/proposals/4/image/",
      vote_count: 2,
      percentage: "100",
      compatibility: "85.25",
      valid_assessments: 3,
    }],
    winners: [{
      proposal_id: 4,
      title: "Design finalista",
      vote_count: 2,
      percentage: "100",
    }],
    is_tie: false,
    survey_results: {
      respondent_count: 1,
      questions: [{
        id: "base_color",
        question: "Cor-base preferida",
        respondent_count: 1,
        options: [{
          value: "BLACK",
          label: "Preta",
          response_count: 1,
          percentage: "100",
        }],
      }],
    },
    compatibility_status: "available",
    compatibility_winners: [{
      proposal_id: 4,
      title: "Design finalista",
      valid_assessments: 3,
      average_compatibility: "85.25",
      status: "available",
    }],
    compatibility_is_tie: false,
  }));
  try {
    const results = await api.getStudentResults();
    assert.equal(results.proposals[0].percentage, 100);
    assert.equal(results.proposals[0].compatibility, 85.25);
    assert.equal(results.proposals[0].image_url, "/proposals/4/image/");
    assert.equal(results.survey_results.questions[0].options[0].percentage, 100);
    assert.equal(results.total_votes, 2);
    assert.equal("student" in results.proposals[0], false);
  } finally {
    restore();
  }
});

test("expired student sessions notify the authentication context", async () => {
  let expired = false;
  const handleExpired = () => {
    expired = true;
  };
  window.addEventListener("threeshirt:session-expired", handleExpired);
  const restore = setFetch(async () => mockResponse(
    { error: "Autenticação necessária." },
    401,
  ));
  try {
    await assert.rejects(api.getStudentDashboard());
    assert.equal(expired, true);
  } finally {
    restore();
    window.removeEventListener("threeshirt:session-expired", handleExpired);
  }
});

test("student dashboard reads the proposal submission stage from the API", async () => {
  const restore = setFetch(async () => mockResponse({
    survey_open: false,
    proposal_submission_open: false,
    voting_open: false,
    results_release_authorized: false,
    has_preferences: false,
    has_voted: false,
  }));
  try {
    const dashboard = await api.getStudentDashboard();
    assert.equal(dashboard.proposal_submission_open, false);
  } finally {
    restore();
  }
});

test("session parsing preserves student, student administrator, and admin roles", async () => {
  for (const role of ["student", "student_admin", "admin"]) {
    const restore = setFetch(async () => mockResponse({
      authenticated: true,
      user: { id: 21, email: `${role}@school.test`, role },
    }));
    try {
      const session = await api.getSession();
      assert.equal(session.authenticated, true);
      assert.equal(session.user.role, role);
    } finally {
      restore();
    }
  }
});

test("admin API preserves hybrid roles and accepts results release controls", async () => {
  const calls = [];
  const restore = setFetch(async (url, init) => {
    calls.push({ url: String(url), init });
    if (String(url).endsWith("/users/")) {
      return mockResponse({
        users: [{
          id: 32,
          email: "hybrid@school.test",
          full_name: "Estudante Admin",
          role: "STUDENT_ADMIN",
          is_active: true,
          is_staff: false,
          has_password: true,
          has_participation: false,
        }],
      });
    }
    return mockResponse({ message: "Atualizado." });
  });
  try {
    const users = await api.getAdminUsers();
    assert.equal(users[0].role, "STUDENT_ADMIN");
    await api.updateAdminUserRole(32, "STUDENT_ADMIN", "Aprovado");
    await api.updateAdminStage("results_release", "open");
    assert.deepEqual(JSON.parse(calls[1].init.body), {
      role: "STUDENT_ADMIN",
      reason: "Aprovado",
    });
    assert.deepEqual(JSON.parse(calls[2].init.body), {
      stage: "results_release",
      action: "open",
    });
  } finally {
    restore();
  }
});

test("account reset API sends a reasoned preview and one-time confirmation", async () => {
  const calls = [];
  const restore = setFetch(async (url, init) => {
    calls.push({ url: String(url), init });
    if (String(url).endsWith("/maintenance/preview/")) {
      return mockResponse({
        challenge_token: "server-issued-token",
        expires_at: "2026-10-31T12:00:00-03:00",
        action: "RESET_USER_BASIC",
        target: { id: 32, label: "Estudante", email: "student@school.test" },
        summary: {
          account_preserved: 1,
          email_preserved: 1,
          profiles_reset: 1,
          responses_preserved: 3,
          votes_preserved: 4,
          proposals_preserved: 2,
        },
        warning: "",
      });
    }
    return mockResponse({
      message: "Conta reinicializada; o histórico eleitoral foi preservado.",
      affected: { votes_preserved: 4, proposals_preserved: 2 },
      warning: "",
      file_cleanup_pending: false,
    });
  });
  try {
    const preview = await api.previewAdminMaintenance(
      "reset_user_basic",
      32,
      "Conta transferida",
    );
    assert.equal(preview.action, "RESET_USER_BASIC");
    assert.equal(preview.summary.account_preserved, 1);
    await api.confirmAdminMaintenance(preview.challenge_token);
    assert.deepEqual(JSON.parse(calls[0].init.body), {
      action: "reset_user_basic",
      target_id: 32,
      reason: "Conta transferida",
    });
    assert.deepEqual(JSON.parse(calls[1].init.body), {
      challenge_token: "server-issued-token",
    });
    assert.equal(calls[0].init.headers.get("X-CSRFToken"), "test-token");
    assert.equal(calls[1].init.headers.get("X-CSRFToken"), "test-token");
  } finally {
    restore();
  }
});

test("election reset API confirms the exact phrase and returns verified backup metadata", async () => {
  const calls = [];
  const restore = setFetch(async (url, init) => {
    calls.push({ url: String(url), init });
    if (String(url).endsWith("/maintenance/preview/")) {
      return mockResponse({
        challenge_token: "reset-token",
        expires_at: "2026-11-08T12:00:00-03:00",
        action: "RESET_ELECTION",
        target: { label: "Todos os dados eleitorais" },
        summary: {
          proposals: 1,
          votes: 0,
          preference_responses: 2,
          participation_identities_preserved: 4,
          result_snapshots_preserved: 1,
        },
        warning: "Identidades e snapshots serão preservados.",
      });
    }
    return mockResponse({
      message: "Dados eleitorais reiniciados.",
      affected: { proposals: 1 },
      warning: "",
      file_cleanup_pending: false,
      backup: {
        path: "backups/maintenance/backup.sqlite3",
        sha256: "a".repeat(64),
        integrity: "ok",
      },
    });
  });
  try {
    const preview = await api.previewAdminMaintenance(
      "reset_election",
      undefined,
      "Encerramento autorizado.",
    );
    assert.equal(preview.action, "RESET_ELECTION");
    const result = await api.confirmAdminMaintenance(
      preview.challenge_token,
      "REINICIAR ELEIÇÃO",
    );
    assert.deepEqual(JSON.parse(calls[0].init.body), {
      action: "reset_election",
      target_id: null,
      reason: "Encerramento autorizado.",
    });
    assert.deepEqual(JSON.parse(calls[1].init.body), {
      challenge_token: "reset-token",
      confirmation_phrase: "REINICIAR ELEIÇÃO",
    });
    assert.equal(result.backup.integrity, "ok");
  } finally {
    restore();
  }
});

test("API errors include backend field validation messages", async () => {
  const restore = setFetch(async () => mockResponse({
    error: "Verifique os dados enviados.",
    fields: {
      title: ["Este campo é obrigatório."],
      image: ["Formato inválido."],
    },
  }, 400));
  try {
    await assert.rejects(
      api.castStudentVote(12),
      (error) =>
        error instanceof api.ApiError &&
        error.message.includes("title: Este campo é obrigatório.") &&
        error.message.includes("image: Formato inválido."),
    );
  } finally {
    restore();
  }
});
