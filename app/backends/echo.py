"""Trivial working backends so the app runs end-to-end out of the box.

These are the reference implementations of :class:`~app.backends.base.AgentBackend`.
They have no external dependencies and no side effects beyond logging, so a fresh
clone dispatches a real event to a real backend with zero configuration. Replace
them with your own — an LLM call, a subprocess agent, a relay — without touching
the dispatcher.
"""

from __future__ import annotations

import logging

from app.backends.base import AgentBackend, Result
from app.lib.events import Event
from app.lib.prompt_safety import wrap_user_content

logger = logging.getLogger("dispatch.backend.echo")


class EchoBackend(AgentBackend):
    """Echoes the event's user text back as the reply.

    Demonstrates the contract end-to-end while staying dependency-free. Note it
    runs the user text through :func:`wrap_user_content` first — even an echo
    backend models the prompt-injection envelope an LLM backend would need, so
    the safety pattern is visible at the seam rather than bolted on later.
    """

    name = "echo"

    async def handle(self, event: Event) -> Result:
        safe_text = wrap_user_content(event.text)
        logger.info(
            "echo backend handling session=%s action=%s", event.session_id, event.action
        )
        if not event.text:
            return Result.failure(
                "No message text to echo.", detail="empty event.text"
            )
        return Result.success(
            output=f"echo: {event.text}",
            detail=f"wrapped_len={len(safe_text)}",
        )


class LogBackend(AgentBackend):
    """Logs the event and acknowledges it. The minimal do-nothing backend —
    useful as a default route or for smoke-testing intake without side effects.
    """

    name = "log"

    async def handle(self, event: Event) -> Result:
        logger.info(
            "log backend: session=%s action=%s issue=%s labels=%s",
            event.session_id, event.action, event.issue_id, list(event.labels),
        )
        return Result.success(output="logged", detail="no-op acknowledgement")
