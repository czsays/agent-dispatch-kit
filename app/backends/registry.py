"""Backend registry — maps backend *names* to live :class:`AgentBackend` instances.

A persona's ``backend:`` field and the label routing table both reference a
backend by name; this is where names resolve to instances. The default set ships
the dependency-free reference backends so the app runs out of the box. Replace
or extend ``build_default_backends`` to wire in your own.
"""

from __future__ import annotations

from app.backends.base import AgentBackend
from app.backends.echo import EchoBackend, LogBackend


def build_default_backends() -> dict[str, AgentBackend]:
    """The out-of-the-box backend set. Keyed by ``backend.name``."""
    backends: list[AgentBackend] = [EchoBackend(), LogBackend()]
    return {b.name: b for b in backends}
