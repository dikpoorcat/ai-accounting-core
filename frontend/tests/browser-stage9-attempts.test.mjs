import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

const { recordRefreshAttempt } = createRequire(import.meta.url)("./browser-stage9-hot-refresh.cjs");

test("refresh attempts preserve a timed timeout after earlier successful samples", async () => {
  const attempts = [];
  await recordRefreshAttempt(attempts, { page: "brief", kind: "sample", iteration: 1 },
    async () => 520, async () => null);
  const error = new Error("Timeout 30000ms exceeded at http://localhost/?ticket=private");
  error.name = "TimeoutError";
  await assert.rejects(recordRefreshAttempt(attempts, { page: "brief", kind: "sample", iteration: 2 },
    async attempt => { attempt.phase = "render"; throw error; },
    async () => ({ started: true, elapsed_ms: 30001 })), actual => actual === error);
  assert.equal(attempts[0].status, "complete");
  assert.equal(attempts[0].elapsed_ms, 520);
  assert.equal(attempts[1].status, "failed");
  assert.equal(attempts[1].phase, "render");
  assert.equal(attempts[1].elapsed_ms, 30001);
  assert.equal(attempts[1].error.type, "TimeoutError");
  assert(!JSON.stringify(attempts).includes("private"));
});

test("preparation failures and post-render validation failures remain distinct", async () => {
  const attempts = [], error = new Error("invalid payload");
  await assert.rejects(recordRefreshAttempt(attempts, { kind: "warmup" },
    async () => { throw error; }, async () => ({ started: false })), error);
  await assert.rejects(recordRefreshAttempt(attempts, { kind: "sample" },
    async attempt => { attempt.phase = "validate"; attempt.elapsed_ms = 450; throw error; },
    async () => ({ started: true, elapsed_ms: 450 })), error);
  assert.equal(attempts[0].status, "preparation_failed");
  assert.equal(attempts[0].elapsed_ms, null);
  assert.equal(attempts[1].status, "failed");
  assert.equal(attempts[1].elapsed_ms, 450);
});
