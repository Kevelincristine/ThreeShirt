import assert from "node:assert/strict";
import test from "node:test";
import {
  isEmailSyntaxValid,
  isLoginCodeAccepted,
  requestLoginCodeForEmail,
} from "../src/services/loginCodeRequest.ts";

test("invalid email is rejected without calling the API", async () => {
  let requestCalled = false;
  const result = await requestLoginCodeForEmail("not-an-email", async () => {
    requestCalled = true;
    throw new Error("API must not be called");
  });

  assert.equal(isEmailSyntaxValid("not-an-email"), false);
  assert.equal(requestCalled, false);
  assert.equal(result.state, "invalid_email");
});

test("valid email is normalized and awaits the backend contract", async () => {
  let resolveRequest;
  const pendingRequest = new Promise((resolve) => {
    resolveRequest = resolve;
  });
  let requestedEmail;
  let completed = false;
  const resultPromise = requestLoginCodeForEmail(
    "  Student@School.edu  ",
    (email) => {
      requestedEmail = email;
      return pendingRequest;
    },
  ).then((result) => {
    completed = true;
    return result;
  });

  await Promise.resolve();
  assert.equal(requestedEmail, "student@school.edu");
  assert.equal(completed, false);

  resolveRequest({
    state: "send_failed",
    message: "Falha segura.",
  });
  const result = await resultPromise;
  assert.equal(result.state, "send_failed");
});

test("only backend-confirmed enter_code permits the code step", () => {
  assert.equal(isLoginCodeAccepted("enter_code"), true);
  for (const state of [
    "unauthorized",
    "account_unavailable",
    "send_failed",
    "rate_limited",
    "invalid_email",
  ]) {
    assert.equal(isLoginCodeAccepted(state), false, state);
  }
});
