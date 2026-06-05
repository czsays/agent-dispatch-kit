"""Runtime configuration, sourced from the environment.

Everything tunable lives here so the rest of the app takes a ``Config`` rather
than reaching into ``os.environ`` directly (which keeps it testable). Load once
at startup via :func:`Config.from_env`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping


def _default_label_routes() -> dict[str, str]:
    # route/<name> -> backend name. Ships pointing at the dependency-free
    # reference backends so the app routes end-to-end with no config.
    return {"echo": "echo", "log": "log"}


def _parse_label_routes(raw: str | None) -> dict[str, str]:
    """Parse ``WEBHOOK_LABEL_ROUTES`` into a ``route-name -> backend`` map.

    Format: a comma-separated list of ``name:backend`` pairs, e.g.
    ``echo:echo,log:log,triage:my-agent``. An unset/blank value yields the
    default reference routes. Malformed pairs (no ``:``, empty name/backend) are
    skipped with no crash — a single fat-fingered entry shouldn't take the whole
    table down.
    """
    if not raw or not raw.strip():
        return _default_label_routes()
    routes: dict[str, str] = {}
    for pair in raw.split(","):
        name, sep, backend = pair.partition(":")
        name, backend = name.strip(), backend.strip()
        if sep and name and backend:
            routes[name] = backend
    return routes or _default_label_routes()


@dataclass(frozen=True)
class Config:
    """Resolved runtime configuration."""

    # The header carrying the hex HMAC-SHA256 signature of the raw body.
    signature_header: str = "x-signature"
    # The fallback/global signing secret used when no persona-specific secret
    # is selected. Empty string => fail-closed (no global secret configured).
    signing_secret: str = ""
    # Replay-window half-width, in seconds (symmetric ±window).
    replay_window_seconds: int = 60
    # Max body bytes the webhook handler will read before rejecting (413).
    max_body_bytes: int = 1 * 1024 * 1024
    # In-memory async queue depth between the ack handler and the worker.
    queue_max: int = 128
    # route/<name> -> backend name. Configurable via WEBHOOK_LABEL_ROUTES.
    label_routes: dict[str, str] = field(default_factory=_default_label_routes)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        e = os.environ if env is None else env
        return cls(
            signature_header=e.get("WEBHOOK_SIGNATURE_HEADER", "x-signature").lower(),
            signing_secret=e.get("WEBHOOK_SIGNING_SECRET", ""),
            replay_window_seconds=int(e.get("WEBHOOK_REPLAY_WINDOW_SECONDS", "60")),
            max_body_bytes=int(e.get("WEBHOOK_MAX_BODY_BYTES", str(1 * 1024 * 1024))),
            queue_max=int(e.get("DISPATCH_QUEUE_MAX", "128")),
            label_routes=_parse_label_routes(e.get("WEBHOOK_LABEL_ROUTES")),
        )
