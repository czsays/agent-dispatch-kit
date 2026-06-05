"""The dispatch router.

Decides *which backend* handles a normalized :class:`~app.lib.events.Event`,
then runs it. The routing decision is a pure function (:func:`decide`) so it is
trivial to unit-test; execution (:func:`dispatch_event`) is the thin async shell
that runs the chosen backend and normalizes its outcome.

Routing precedence (highest first), genericized from the original's
label/mention model:

  1. **Persona binding.** If the event names a ``persona_id`` that resolves to
     an *available* persona, that persona's ``backend`` wins. (An unavailable
     persona routes to ``elicit`` — hold for a human — never to a worker.)
  2. **Routing label.** A single recognized ``route/<name>`` label selects the
     backend mapped to ``<name>``. Zero, multiple, or unknown labels → ``elicit``.
  3. **Fallback.** No persona and no usable label → ``elicit``.

``elicit`` is not a backend — it is the "ask a human / hold state" sentinel.
The dispatcher surfaces it as a failure Result with the reason, mirroring the
original's "route to CZ" branch without any of the workspace specifics.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.backends.base import AgentBackend, Result
from app.config import Config
from app.lib.events import Event
from app.lib.registry import Registry

logger = logging.getLogger("dispatch.router")

# Sentinel "no backend — ask a human / hold state" target.
ELICIT = "elicit"

ROUTE_LABEL_PREFIX = "route/"


@dataclass(frozen=True)
class Decision:
    """The routing verdict for one event."""

    backend: str  # a backend name, or ELICIT
    reason: str


def _route_label(labels: tuple[str, ...]) -> tuple[str | None, str | None]:
    """Return (route-name, error-reason). Exactly one ``route/<name>`` label is
    required; zero / multiple is ambiguous and yields a reason instead."""
    route_labels = [lbl for lbl in labels if lbl.startswith(ROUTE_LABEL_PREFIX)]
    if len(route_labels) == 1:
        return route_labels[0][len(ROUTE_LABEL_PREFIX):], None
    if len(route_labels) > 1:
        return None, f"multiple route/* labels ({', '.join(sorted(route_labels))}); pick one"
    return None, "no route/* label"


def decide(event: Event, registry: Registry, config: Config) -> Decision:
    """Pure routing decision. No I/O — feed it an Event, a Registry, and Config."""
    # 1. Persona binding wins when it resolves to an available persona.
    if event.persona_id:
        persona = registry.get(event.persona_id)
        if persona is None:
            return Decision(ELICIT, f"persona {event.persona_id!r} is not registered")
        if not persona.available:
            return Decision(
                ELICIT,
                f"persona {persona.slug!r} unavailable: {persona.unavailable_reason}",
            )
        return Decision(persona.backend, f"persona {persona.slug!r} -> {persona.backend}")

    # 2. A single recognized route/* label.
    route_name, err = _route_label(event.labels)
    if route_name is not None:
        backend = config.label_routes.get(route_name)
        if backend is None:
            return Decision(ELICIT, f"unknown route label `route/{route_name}`")
        return Decision(backend, f"label `route/{route_name}` -> {backend}")

    # 3. Fallback — hold for a human.
    return Decision(ELICIT, err or "unroutable event")


async def dispatch_event(
    event: Event,
    *,
    registry: Registry,
    backends: dict[str, AgentBackend],
    config: Config,
) -> Result:
    """Route ``event`` to a backend and run it. Always returns a :class:`Result`.

    An ``elicit`` decision, an unknown backend, or an exception raised inside a
    backend all collapse to a failure ``Result`` — the caller (the listener's
    worker loop) never has to handle exceptions from here, only the ok flag.
    """
    decision = decide(event, registry, config)
    logger.info(
        "dispatch session=%s backend=%s reason=%s",
        event.session_id, decision.backend, decision.reason,
    )

    if decision.backend == ELICIT:
        return Result.failure(
            output="This needs a human — holding.", detail=decision.reason
        )

    backend = backends.get(decision.backend)
    if backend is None:
        return Result.failure(
            output="Misconfigured route — no such backend.",
            detail=f"backend {decision.backend!r} is not registered",
        )

    try:
        return await backend.handle(event)
    except Exception as exc:  # noqa: BLE001 — convert any backend error to a Result
        logger.exception(
            "backend %r raised on session=%s", decision.backend, event.session_id
        )
        return Result.failure(
            output="The backend hit an error handling this.",
            detail=f"{type(exc).__name__}: {exc}",
        )
