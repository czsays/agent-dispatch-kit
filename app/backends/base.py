"""The pluggable backend seam.

A *backend* is whatever actually handles a normalized :class:`~app.lib.events.Event`
— inline business logic, a subprocess agent, a call out to an LLM, a relay to
another service. The dispatcher only knows this interface; it never knows how a
backend does its work. That is the whole point of the extraction: **bring your
own backend.**

To add one:
  1. Implement :class:`AgentBackend` (a single ``async def handle(event) -> Result``).
  2. Register it by name (see ``app.backends.registry.build_default_backends``).
  3. Point a persona's ``backend:`` field at that name in ``personas.yaml``,
     or map a routing label to it in ``app.config``.

See ``docs/adr/0004-pluggable-backend-routing.md`` for the routing model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.lib.events import Event


@dataclass(frozen=True)
class Result:
    """The outcome of handling one event.

    ``ok`` is the success flag the dispatcher uses to choose follow-up state.
    ``output`` is the human-facing reply (e.g. what you'd post back to the
    thread). ``detail`` carries machine-facing context (error class, routing
    notes) for logs and tests.
    """

    ok: bool
    output: str
    detail: str = ""

    @classmethod
    def success(cls, output: str, detail: str = "") -> "Result":
        return cls(ok=True, output=output, detail=detail)

    @classmethod
    def failure(cls, output: str, detail: str = "") -> "Result":
        return cls(ok=False, output=output, detail=detail)


@runtime_checkable
class AgentBackend(Protocol):
    """A handler for normalized events. Implement this to plug in your agent.

    ``name`` is the stable identifier a persona's ``backend:`` field and the
    label routing table reference. ``handle`` must be a coroutine and must not
    raise for ordinary failures — return ``Result.failure(...)`` instead, so the
    dispatcher can choose follow-up state. (Unexpected exceptions are still
    caught by the dispatcher and converted to a failure Result.)
    """

    name: str

    async def handle(self, event: Event) -> Result:
        ...
