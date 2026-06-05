# ADR-0001: HMAC-SHA256 webhook verification with a symmetric replay window

Date: 2026-06-05
Status: Accepted

## Context

The service is a publicly reachable HTTP endpoint that receives webhooks and, on
each one, takes real action (it dispatches to an agent backend that may call an
LLM, spawn a subprocess, or write to an external system). Anyone who can reach
the endpoint can therefore trigger work. The endpoint needs a trust boundary at
the HTTP edge: only callers holding the shared signing secret should be able to
get past it, and a captured-then-replayed request should not work indefinitely.

The webhook sender signs the **raw request body** with a shared secret using
HMAC-SHA256 and attaches the hex digest in a header. The body also carries a
millisecond timestamp. A subtlety: a routing key the receiver needs (which
persona/tenant the event is for) lives *inside* the signed body, so the receiver
must read — parse — the body before it can select the right secret to verify
against.

## Decision

A small, pure verification module (`app/lib/webhook_verify.py`) with four
primitives:

1. **`verify(raw_body, signature_header, secret) -> bool`** — recompute the
   HMAC over the raw bytes and compare with `hmac.compare_digest` (constant-time;
   never `==`). Fail closed on an empty secret, empty signature, or empty body —
   none of those should ever pass. An unset secret verifying would be
   catastrophic: an attacker computes HMAC with `secret=""` and walks in.

2. **`verify_any(raw_body, signature, secrets) -> bool`** — True if *any*
   candidate secret in the selected set verifies. It runs each candidate through
   `verify`, so the constant-time compare and empty-secret/empty-body guards hold
   per secret. An empty candidate set fails closed. `verify_any` is the
   primitive; *which* secrets it gets handed is the load-bearing decision below.

3. **`within_replay_window(timestamp_ms, window_seconds)`** — reject a payload
   whose timestamp is more than `window_seconds` from now in *either* direction,
   via `abs()`. Symmetric is the point: a past-only check lets an attacker park a
   far-future timestamp and replay it forever.

4. **`sign(...)`** — exposed for tests and for clients that re-sign a payload.

**Scoped secret selection — the trust-boundary core.** The handler does NOT
verify against every secret it knows. It reads the in-body `persona_id` routing
key and scopes the candidate set (`listener._candidate_secrets`):

- **`persona_id` names a registered persona** → verify against *that persona's*
  signing secret only. A webhook claiming persona A but signed with persona B's
  key cannot pass: the verifier is handed A's secret alone, so B's signature
  fails — no cross-persona signature confusion, and no routing a B-signed
  payload to A's backend.
- **The named persona is registered but its secret env var is unset** → **fail
  closed** (empty candidate set, 401). It does *not* fall through to try-all and
  borrow a sibling's secret, which would let some other persona's key verify a
  webhook that explicitly claims to be this (misconfigured) one.
- **`persona_id` is absent or names no registered persona** → fall back to the
  *try-all* set: the global `WEBHOOK_SIGNING_SECRET` plus every available
  persona's secret. This is the one set that serves **key rotation** (old + new
  global secret both valid during a cutover) and the **no-routing-key** sender.
  Try-all is the unscoped fallback, never the path for a recognized persona.

**Parse-before-verify, verify-before-act.** The handler parses the JSON body to
read `persona_id`, *then* selects the scoped secret, *then* verifies, *then*
acts. Parsing is inert — no action is taken on the payload until the HMAC passes.
`persona_id` is itself inside the HMAC-protected body, so a tampered value only
ever selects the wrong (or no) secret and fails the verify. To bound the cost of
parsing unauthenticated input, the handler enforces a body-size cap (413) before
reading the body; it also rejects a non-object JSON body (400) before touching
any field.

## Consequences

- **+** A clean, fail-closed trust boundary that's pure and unit-testable
  independent of the web framework.
- **+** Scoping the candidate set by `persona_id` makes per-persona rejection
  *precise*: a payload claiming a persona is checked against that persona's key
  alone, so a sibling's secret can never verify it. Multi-tenant fan-out is the
  *selection*, not a try-all over every tenant's secret.
- **+** Key rotation still falls out of `verify_any` over the global old+new
  pair (the unscoped fallback set), with no special-casing.
- **+** The constant-time compare closes the timing side-channel that a naive
  `==` opens (forge the signature prefix-by-prefix).
- **−** Parsing unauthenticated JSON before verifying is a (bounded) attack
  surface; mitigated by the size cap, but it exists because the secret selector
  lives in the signed body. An endpoint that didn't need in-body routing could
  verify before parsing.
- **−** The replay window trades a little clock-skew tolerance for replay
  resistance; the sender and receiver clocks must stay within the window.

## Alternatives considered

- **TLS / network ACLs only.** Encrypts transit and limits reachability but does
  not authenticate the *payload* — anyone past the network boundary can forge a
  request. HMAC authenticates the body itself.
- **A nonce store for exactly-once replay defense.** Stronger than a time
  window, but needs durable shared state. The time window is stateless and
  sufficient when paired with idempotent downstream handling.
- **Verify before parse.** Cleaner ordering, but impossible here: the secret
  selector is inside the signed body, so the body must be parsed first.
