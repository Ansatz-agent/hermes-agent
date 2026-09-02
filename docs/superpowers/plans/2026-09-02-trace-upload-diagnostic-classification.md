# Trace Upload Diagnostic Classification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Trace upload log's ambiguous `network` bucket with four safe diagnostic categories while retaining durable retry behavior and keeping low-level details out of the user-facing UI.

**Architecture:** Extend `TraceUploadEvent` with a bounded `failureCode` plus batch ID, elapsed milliseconds, HTTP status, and sanitized server request ID. Classify failures once inside `TraceForwarder.sendForReceipt`, where credential, network, response-status, and receipt-validation phases are visible; callers keep their existing retry/quarantine decisions without emitting duplicate events. `main.ts` formats the event as an internal `[trace]` log line and does not expose payloads, tokens, or raw exception text.

**Tech Stack:** TypeScript, Vitest, Electron main-process logging, Fetch `Response` headers.

---

## Task 1: Add red tests for the four diagnostic categories

**Files:**
- Modify `apps/desktop/electron/trace-forwarder.test.ts`.

- [ ] Capture `onUploadEvent` events while pumping one pre-enqueued batch through each failure path: credential loader rejection, thrown fetch/network error, HTTP 503 rejection with `x-request-id`, and HTTP 202 response missing the Trace receipt headers.
- [ ] Assert each event has exactly one category (`credential`, `network`, `http_rejected`, or `missing_receipt`), the same batch ID, the expected HTTP status (`null`, `null`, `503`, or `202`), elapsed milliseconds, and the sanitized request ID (`null`, `null`, `req-http-503`, or `req_missing_receipt`).
- [ ] Run the focused diagnostic tests before changing production code and confirm they fail against the current `{ kind: 'failure', status }` event shape rather than failing to start the test harness.

Expected result: the tests demonstrate the missing observability contract without changing retry or outbox behavior.

## Task 2: Add typed event metadata and classify failures in `TraceForwarder`

**Files:**
- Modify `apps/desktop/electron/trace-forwarder.ts` (`TraceUploadEvent`, `UpstreamFailure`, `sendForReceipt`, `requireGatewayReceipt`, and request-ID helper).

- [ ] Define `TraceUploadFailureCode = 'credential' | 'network' | 'http_rejected' | 'missing_receipt'` and add `failureCode`, `batchId`, `elapsedMs`, and nullable `requestId` fields to upload events while retaining `status` for compatibility.
- [ ] Extend `UpstreamFailure` with the failure code and request ID; have `requireGatewayReceipt` classify non-2xx responses as `http_rejected` and 2xx responses without matching `x-trace-batch-id`/`x-trace-receipt` as `missing_receipt`.
- [ ] In `sendForReceipt`, start a clock at entry, track credential/network/response phases, emit one success or failure event with elapsed time and batch ID, and wrap non-`UpstreamFailure` errors as `network` unless they are credential validation/provider failures.
- [ ] Read `x-request-id` (falling back to `request-id`) only from the final HTTP response. Trim it, replace characters outside `[A-Za-z0-9._:-]` with `_`, cap it at 128 characters, and return `null` when absent/empty so logs cannot contain control characters or arbitrary response text.
- [ ] Remove the caller-side failure emissions from `handleCloudFailure` and `pumpUntilBlocked`; those paths must continue applying the existing retry/quarantine policy after `sendForReceipt` has emitted its single diagnostic event.

Expected result: every upload attempt is classified once, and existing retry timing, terminal revocation, and outbox acknowledgement semantics are unchanged.

## Task 3: Format the classified event in the internal desktop log

**Files:**
- Modify `apps/desktop/electron/main.ts` in `createDesktopTraceSession`'s `onUploadEvent` callback.

- [ ] Log success as `batch_id`, `outcome`, `http_status`, `elapsed_ms`, and `request_id`.
- [ ] Log failure as `batch_id`, `failure`, `http_status`, `elapsed_ms`, and `request_id`, followed by the existing durable retry/quarantine note.
- [ ] Keep the callback internal to `rememberLog`; do not add a renderer notification, token, payload, raw exception message, or user conversation content.

Expected result: operators can distinguish credential, network, HTTP, and missing-receipt failures from `desktop.log` without exposing sensitive data to users.

## Task 4: Verify and commit the diagnostic change

- [ ] Run the focused forwarder diagnostic tests and confirm all four categories pass.
- [ ] Run `./node_modules/.bin/vitest run apps/desktop/electron/trace-forwarder.test.ts apps/desktop/electron/trace-continuity.integration.test.ts --reporter=dot` with local-network approval when needed.
- [ ] Run `npm run --workspace apps/desktop typecheck` and `git diff --check`.
- [ ] Inspect the diff to confirm only the forwarder, main logging, diagnostic tests, and this plan changed; retain unrelated untracked files untouched.
- [ ] Commit with `feat: classify trace upload diagnostics` on `fix/trace-inprocess-resume-test`; do not merge or modify `main`.

Expected result: the new diagnostic contract is covered by tests and available for future incident triage without changing the user-facing upload experience.
