"""The normalized event model.

A raw webhook payload is provider-shaped and messy. The dispatcher and every
backend speak in terms of a single normalized ``Event`` instead, so backends
never have to know the wire format. ``parse_event`` is the one place that knows
the (generic, documented) payload shape; swap it to adapt to a real provider.

The reference payload shape this parser understands:

    {
        "action": "created" | "prompted" | ...,   # what happened
        "timestamp": 1717600000000,                # ms since epoch (replay check)
        "session": {                               # the conversational session
            "id": "abc",
            "issue_id": "iss-1",                   # the work item, optional
            "actor_id": "user-1",                  # who triggered it, optional
            "text": "..."                          # the user's message
        },
        "labels": ["route/echo"],                  # routing hints, optional
        "persona_id": "persona-a"                  # fan-out target, optional
    }

``Event.text`` is untrusted user content. Backends that feed it to an LLM must
wrap it in a delimiter envelope first — see
``docs/adr/0002-prompt-injection-delimiters.md`` and ``app.lib.prompt_safety``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class EventParseError(ValueError):
    """Raised when a payload is missing a field required to build an Event."""


@dataclass(frozen=True)
class Event:
    """One normalized inbound event."""

    session_id: str
    action: str
    text: str | None
    issue_id: str | None = None
    actor_id: str | None = None
    persona_id: str | None = None
    labels: tuple[str, ...] = ()
    raw: dict = field(default_factory=dict)


def parse_event(payload: dict) -> Event:
    """Normalize a raw webhook payload into an :class:`Event`.

    Raises :class:`EventParseError` if ``session.id`` is absent — without a
    session id there is nothing to address a reply to.
    """
    session = payload.get("session") or {}
    session_id = session.get("id")
    if not session_id:
        raise EventParseError("payload missing session.id")

    labels_raw = payload.get("labels") or []
    labels = tuple(str(label) for label in labels_raw if label)

    # Coerce untrusted scalar fields to ``str``/``None``. A webhook can carry any
    # JSON type here (a list, a dict, an int); without coercion a non-str
    # ``persona_id`` reaches the router's dict lookup / string ops and raises a
    # ``TypeError`` *outside* the backend's try/except, dropping the event
    # silently. ``str(x) if x else None`` keeps a present-but-falsy value (``""``,
    # ``0``) as ``None`` and stringifies everything else.
    return Event(
        session_id=str(session_id),
        action=str(payload.get("action") or "unknown"),
        text=_coerce_str(session.get("text")),
        issue_id=_coerce_str(session.get("issue_id")),
        actor_id=_coerce_str(session.get("actor_id")),
        persona_id=_coerce_str(payload.get("persona_id")),
        labels=labels,
        raw=payload,
    )


def _coerce_str(value: object) -> str | None:
    """Stringify an untrusted scalar field, mapping a falsy value to ``None``."""
    return str(value) if value else None
