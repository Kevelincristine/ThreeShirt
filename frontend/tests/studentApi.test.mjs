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
    assert.equal(calls[1].url, "http://localhost:5173/api/election/student/proposals/");
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
