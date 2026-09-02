# Trace In-Process Resume Regression Test Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic integration regression test proving that a running desktop Trace session automatically uploads a durably queued batch after the upstream gateway returns, without app restart, re-login, or a manual recovery trigger.

**Architecture:** Extend only the existing integration-test harness so the retry jitter source can be fixed for the test. Add one offline-to-online test using the same `ControllableGateway` and `TraceHarness` instance throughout. The test observes the existing retry timer and validates durable drain, receipt acceptance, batch identity, and payload digest. No production retry, outbox, authentication, or server behavior changes are planned.

**Tech Stack:** TypeScript, Vitest, Node HTTP test gateway, existing `TraceForwarder`/`TraceRecoveryLifecycle` integration harness.

---

## Task 1: Make retry jitter injectable in the integration harness

**Files:**
- Modify `apps/desktop/electron/trace-continuity.integration.test.ts` (`launchTraceHarness` option type and `TraceForwarder` construction)

- [ ] Add an optional `random?: () => number` field to the `launchTraceHarness` options object.
- [ ] Pass that field through as `random: options.random` when constructing `TraceForwarder`; leave all existing callers unchanged so production defaults and current tests retain their behavior.
- [ ] Run `npm run --workspace apps/desktop typecheck` and confirm the helper change introduces no new TypeScript errors.

Expected result: the test harness can select a stable non-zero retry jitter without adding a production-only hook.

## Task 2: Add the same-process offline-to-online regression test

**Files:**
- Modify `apps/desktop/electron/trace-continuity.integration.test.ts` near the existing restart-recovery continuity tests.

- [ ] Start `ControllableGateway` in `offline` mode and launch one authenticated harness with `random: () => 0.5`.
- [ ] Post one `payload('in-process-resume')` body and wait with the existing `waitFor` helper until the outbox reports one pending item and the gateway has recorded at least two `unavailable` attempts; the second failure proves the recovery pump has seeded its retry timer after the admission fast path.
- [ ] Record the last unavailable attempt's batch ID and digest, then call `gateway.setOnline(true)` on the same gateway instance without quitting/recreating the harness.
- [ ] Wait for the existing scheduled retry to drain the outbox; do not call `harness.trigger()`, restart, or re-login after switching online.
- [ ] Assert pending count is zero, exactly one logical batch exists, an accepted/duplicate receipt uses the recorded batch ID, and the stored gateway digest/body match the original payload (`sha256(body)`).
- [ ] Keep cleanup in the test's `finally` block using the existing `cleanupContinuityTest` helper.

Expected result: the new test passes if in-process retry recovery works. If it fails, preserve the failure output as diagnostic evidence and do not mask it by adding a manual trigger or restart.

## Task 3: Verify the regression and classify any failure

**Files:**
- No additional files unless the focused test exposes a harness defect that must be corrected to express the approved design.

- [ ] Run the focused test first:
  `./node_modules/.bin/vitest run apps/desktop/electron/trace-continuity.integration.test.ts -t "same running Trace session resumes an offline durable batch after the Gateway returns online" --reporter=verbose`
- [ ] If the focused test passes, run the related continuity/forwarder suites:
  `./node_modules/.bin/vitest run apps/desktop/electron/trace-continuity.integration.test.ts apps/desktop/electron/trace-forwarder.test.ts --reporter=dot`
- [ ] If localhost binding is denied by the sandbox, rerun the same command with the required local-network approval rather than changing the test to avoid its real HTTP gateway.
- [ ] For a failure, capture whether the pending item stayed eligible, the retry timer fired, the gateway received a request, or the receipt was persisted; report the classification before proposing production changes.
- [ ] Run `git diff --check` and inspect the final diff to ensure only the approved test harness/test and this plan changed; retain unrelated pre-existing files untouched.

Expected result: a reproducible pass/fail signal for the exact user scenario, with no restart-based false positive.

## Task 4: Commit the test change on the feature branch

- [ ] Stage only `apps/desktop/electron/trace-continuity.integration.test.ts`, `docs/superpowers/specs/2026-09-02-trace-inprocess-resume-test-design.md`, and `docs/superpowers/plans/2026-09-02-trace-inprocess-resume-test.md`; do not stage unrelated working-tree files.
- [ ] Commit with a focused message such as `test: cover in-process trace recovery after gateway outage`.
- [ ] Record the commit hash and verification command results for the handoff. Do not merge or modify `main` in this task.
