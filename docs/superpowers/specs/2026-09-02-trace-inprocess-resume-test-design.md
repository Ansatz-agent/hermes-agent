# Trace same-process recovery regression test

## Problem

The desktop trace path durably stores a failed batch in the encrypted outbox and
uses an in-memory recovery controller to retry it. Existing coverage proves
that a pending batch survives an application restart and that retry backoff is
honored, but it does not prove the user-facing requirement that a running app
automatically resumes upload after the upstream service becomes available.

The observed incident therefore cannot currently distinguish a real
same-process recovery failure from a retry that had not yet become eligible.

## Scope

Add one integration regression test under
`apps/desktop/electron/trace-continuity.integration.test.ts`. The test will not
change production retry timing, outbox format, authentication, or user-facing
behavior.

## Test design

Use the existing `ControllableGateway` and `launchTraceHarness`:

1. Start the gateway offline and launch one authenticated trace harness.
2. Post one trace and wait until the request is durably admitted locally while
   the gateway records at least two unavailable attempts. The second failed
   attempt must come from the recovery pump, ensuring that the in-memory retry
   timer has been scheduled rather than observing only the admission fast path.
3. Switch the same gateway instance to online without quitting or recreating
   the harness.
4. Wait for the existing retry timer to fire and for the outbox to drain; do
   not restart the app or issue a manual recovery trigger after the gateway is
   switched online.
5. Assert that the pending count reaches zero, the gateway accepted the batch,
   and exactly one logical batch exists with the original payload digest and
   batch identity.

The harness will accept a fixed random source for the retry policy. The test
will use a stable mid-range jitter value and will not manually trigger recovery
after switching the gateway online; the existing retry timer must perform the
resume. No production-only hooks or test branches will be added.

## Failure interpretation

- If the test passes, same-process durable recovery is working; the incident
  points to retry timing, credential refresh, connection reuse, or an upstream
  response-contract issue. The test becomes a permanent guard against claiming
  that a restart is required.
- If the test fails, the failure output will identify whether the retry was not
  scheduled, the batch remained ineligible, the request was not sent, or the
  receipt was not persisted. Only then will a production fix be designed.

## Verification

Run the focused trace continuity and forwarder test files, then run the full
desktop test command used by the repository if the focused run is green. The
test must keep the gateway and harness alive throughout the offline-to-online
transition; a restart-based helper is not an acceptable substitute.

## Non-goals

- Reducing the existing exponential backoff cap.
- Persisting retry timers or attempt counters in the outbox journal.
- Adding a health-probe endpoint or UI notification.
- Changing trace payloads, credentials, or server APIs.
