"""Tests for app.dispatch — the routing decision + execution shell."""

from __future__ import annotations

import pytest

from app.backends.base import AgentBackend, Result
from app.backends.echo import EchoBackend, LogBackend
from app.config import Config
from app.dispatch import ELICIT, decide, dispatch_event
from app.lib.events import Event
from app.lib.registry import Persona, Registry


def _persona(slug, backend, *, available=True):
    return Persona(
        slug=slug, display_name=slug.title(), role="worker", backend=backend,
        token_env="TOK", signing_secret_env="SIG",
        capabilities=(), available=available,
        unavailable_reason=None if available else "secret unset",
    )


def _registry(*personas):
    return Registry(personas={p.slug: p for p in personas})


def _event(**kw):
    base = dict(session_id="s1", action="created", text="hi")
    base.update(kw)
    return Event(**base)


CONFIG = Config()  # default label_routes: echo->echo, log->log


# ---------- decide(): persona binding ----------


def test_persona_binding_wins():
    reg = _registry(_persona("responder", "echo"))
    d = decide(_event(persona_id="responder"), reg, CONFIG)
    assert d.backend == "echo"
    assert "responder" in d.reason


def test_unavailable_persona_routes_to_elicit_not_a_backend():
    reg = _registry(_persona("responder", "echo", available=False))
    d = decide(_event(persona_id="responder"), reg, CONFIG)
    assert d.backend == ELICIT
    assert "unavailable" in d.reason


def test_unregistered_persona_routes_to_elicit():
    reg = _registry()
    d = decide(_event(persona_id="ghost"), reg, CONFIG)
    assert d.backend == ELICIT


# ---------- decide(): label routing ----------


def test_single_known_route_label():
    d = decide(_event(labels=("route/echo",)), _registry(), CONFIG)
    assert d.backend == "echo"
    assert d.reason == "label `route/echo` -> echo"


def test_unknown_route_label_elicits():
    d = decide(_event(labels=("route/nope",)), _registry(), CONFIG)
    assert d.backend == ELICIT
    assert "unknown route label" in d.reason


def test_multiple_route_labels_elicit():
    d = decide(_event(labels=("route/echo", "route/log")), _registry(), CONFIG)
    assert d.backend == ELICIT
    assert "multiple route" in d.reason


def test_no_label_no_persona_elicits():
    d = decide(_event(), _registry(), CONFIG)
    assert d.backend == ELICIT


def test_non_route_labels_are_ignored():
    d = decide(_event(labels=("type/bug", "p/high")), _registry(), CONFIG)
    assert d.backend == ELICIT  # no route/* label present


# ---------- dispatch_event(): execution shell ----------

BACKENDS = {"echo": EchoBackend(), "log": LogBackend()}


@pytest.mark.asyncio
async def test_dispatch_routes_to_echo_backend():
    reg = _registry()
    result = await dispatch_event(
        _event(labels=("route/echo",), text="ping"),
        registry=reg, backends=BACKENDS, config=CONFIG,
    )
    assert result.ok is True
    assert result.output == "echo: ping"


@pytest.mark.asyncio
async def test_dispatch_routes_to_log_backend_via_persona():
    reg = _registry(_persona("logger", "log"))
    result = await dispatch_event(
        _event(persona_id="logger"),
        registry=reg, backends=BACKENDS, config=CONFIG,
    )
    assert result.ok is True
    assert result.output == "logged"


@pytest.mark.asyncio
async def test_elicit_is_a_failure_result():
    result = await dispatch_event(
        _event(), registry=_registry(), backends=BACKENDS, config=CONFIG,
    )
    assert result.ok is False
    assert "human" in result.output.lower()


@pytest.mark.asyncio
async def test_unknown_backend_name_is_a_failure_result():
    # A route label pointing at a backend that isn't registered.
    cfg = Config(label_routes={"ghost": "ghost"})
    result = await dispatch_event(
        _event(labels=("route/ghost",)),
        registry=_registry(), backends=BACKENDS, config=cfg,
    )
    assert result.ok is False
    assert "ghost" in result.detail


@pytest.mark.asyncio
async def test_backend_exception_becomes_failure_result():
    class Boom(AgentBackend):
        name = "boom"

        async def handle(self, event: Event) -> Result:
            raise ValueError("kaboom")

    cfg = Config(label_routes={"boom": "boom"})
    result = await dispatch_event(
        _event(labels=("route/boom",)),
        registry=_registry(), backends={"boom": Boom()}, config=cfg,
    )
    assert result.ok is False
    assert "ValueError" in result.detail


@pytest.mark.asyncio
async def test_echo_backend_empty_text_fails():
    result = await dispatch_event(
        _event(labels=("route/echo",), text=None),
        registry=_registry(), backends=BACKENDS, config=CONFIG,
    )
    assert result.ok is False
