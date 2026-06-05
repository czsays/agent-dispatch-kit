# ADR-0004: Pluggable backends behind a one-method seam, with two routing inputs

Date: 2026-06-05
Status: Accepted

## Context

The whole point of the extraction is *bring your own backend*: the reference
ships a dependency-free echo/log pair, but a real deployment plugs in an LLM
call, a subprocess agent, or a relay. The dispatcher must select among backends
without knowing how any of them work, and the selection has to be testable in
isolation from the web framework and the network.

There are two natural sources of a routing decision in an inbound event:

- the **persona** the event names (`persona_id`), which carries its own
  identity, signing secret, and a `backend:` field; and
- a **routing label** on the event (`route/<name>`), independent of any persona.

The first cut had both half-wired: persona routing worked, but the label table
was hardcoded in `Config` and never env-driven, and personas carried a `labels:`
field that nothing read. That left two dead concepts and an unclear precedence.

## Decision

**The backend seam is a one-method Protocol.** `AgentBackend` is
`async def handle(event) -> Result` plus a stable `name`. Backends are registered
by name in `app/backends/registry.py` (`build_default_backends`); a persona's
`backend:` field and the label table both reference a backend by that name. The
dispatcher only knows this interface — adding a backend is a new class + a
registry entry, no dispatcher change.

**Routing precedence is a pure function** (`dispatch.decide`), so it unit-tests
without I/O:

1. **Persona binding wins.** An event naming a `persona_id` that resolves to an
   *available* persona routes to that persona's `backend`. A registered-but-
   unavailable persona (its secret env var unset) routes to `elicit` — hold for
   a human — never to a worker. An unregistered `persona_id` also elicits.
2. **A single recognized `route/<name>` label.** With no persona, exactly one
   `route/<name>` label selects the backend mapped to `<name>` in the label
   table. Zero, multiple, or unknown labels elicit (ambiguous → ask a human).
3. **Fallback.** No persona and no usable label → `elicit`.

`elicit` is not a backend; it is the "ask a human / hold state" sentinel,
surfaced as a failure `Result` carrying the reason.

**The label table is env-driven, not persona-derived.** A label maps *straight
to a backend*, parsed once at startup from `WEBHOOK_LABEL_ROUTES`
(`name:backend` pairs, comma-separated; unset → the reference `echo`/`log`
defaults). Personas do **not** claim labels: the dead `Persona.labels` field and
`Registry.for_label` lookup were removed. Routing-via-persona and routing-via-
label are two distinct, independently-testable inputs, not one routed through
the other — which keeps the precedence above unambiguous.

## Consequences

- **+** A new backend is a class + a registry line; the dispatcher and listener
  never change. The seam is the only contract.
- **+** `decide` is pure and exhaustively unit-tested (persona present/absent/
  unavailable, one/zero/many/unknown labels), independent of FastAPI and the
  queue.
- **+** One honest source of truth per routing input: persona binding from the
  registry, label routing from `WEBHOOK_LABEL_ROUTES`. No dead `labels:` field
  implying a third, unimplemented model.
- **−** Two routing inputs with a fixed precedence is one more rule to learn than
  a single mechanism would be. The precedence is documented here and pinned by
  tests so it can't drift silently.
- **−** Label routing maps to a backend, not a persona, so a label-routed event
  carries no persona identity (no per-persona token/secret on the worked event).
  That's intentional — label routing is the persona-less path — but a deployment
  that needs identity on every event should route by `persona_id`.

## Alternatives considered

- **Persona-claimed labels (`Persona.labels` + `for_label`).** The original
  half-built model: a persona lists the labels it answers to, and a label
  resolves to a persona, then to its backend. It was never wired into `decide`
  and overlapped confusingly with the direct label→backend table. Dropped in
  favor of the two-distinct-inputs model above (ADR-0003 notes the registry no
  longer carries a `labels` field).
- **A single routing mechanism (labels only, or personas only).** Simpler, but
  each input earns its keep: persona binding carries identity + secret for the
  multi-tenant fan-out (ADR-0001/0003); label routing is the lightweight
  persona-less path. Collapsing to one would lose a real capability.
- **Backend selection inside each backend (a chain of responsibility).** Every
  backend inspects the event and claims it or passes. More flexible, but the
  routing logic scatters across backends and stops being a single pure function
  you can read and test in one place.
