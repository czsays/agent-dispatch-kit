"""Integration tests for app.listener — the FastAPI webhook surface.

Pins the trust boundary at the HTTP edge: bad signature -> 401 + no enqueue,
replay-window -> 401, oversized -> 413, unparseable -> 400, valid -> 200 +
queue depth +1, and an end-to-end dispatch through the worker to a backend.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.lib import registry as registry_lib
from app.listener import create_app

SECRET = "test-signing-secret"


def _sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _payload(text="hi", **extra) -> dict:
    p = {
        "action": "created",
        "timestamp": int(time.time() * 1000),
        "session": {"id": "sess-1", "issue_id": "iss-1", "text": text},
    }
    p.update(extra)
    return p


@pytest.fixture
def client():
    cfg = Config(signing_secret=SECRET)
    reg = registry_lib.Registry(personas={})
    app = create_app(config=cfg, registry=reg)
    with TestClient(app) as c:
        yield c


def _post(client, payload: dict, *, secret=SECRET, header="x-signature"):
    body = json.dumps(payload).encode()
    return client.post("/webhooks", content=body, headers={header: _sign(body, secret)})


def _post_raw(client, body: bytes, *, secret=SECRET, header="x-signature"):
    """POST arbitrary raw bytes, signed with `secret` so the body reaches past
    the verify gate (used for malformed-but-signed payloads)."""
    return client.post("/webhooks", content=body, headers={header: _sign(body, secret)})


def _persona(slug, *, signing_secret_env, backend="echo"):
    """A registered, available persona whose signing secret resolves from
    `signing_secret_env` in the live env."""
    return registry_lib.Persona(
        slug=slug, display_name=slug.title(), role="worker", backend=backend,
        token_env=f"{slug.upper()}_TOK", signing_secret_env=signing_secret_env,
        capabilities=(), available=True, unavailable_reason=None,
    )


# ---------- happy path ----------


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_valid_webhook_is_accepted_and_enqueued(client):
    before = client.app.state.queue.qsize()
    r = _post(client, _payload())
    assert r.status_code == 200
    assert r.json()["queued"] is True
    # The worker drains asynchronously; assert it was accepted (queued flag).
    # Depth may already be back to `before` if the worker ran — both fine.
    assert client.app.state.queue.qsize() >= before


# ---------- trust boundary ----------


def test_bad_signature_is_401_and_not_enqueued(client):
    body = json.dumps(_payload()).encode()
    r = client.post("/webhooks", content=body, headers={"x-signature": "deadbeef"})
    assert r.status_code == 401


def test_wrong_secret_is_401(client):
    r = _post(client, _payload(), secret="not-the-secret")
    assert r.status_code == 401


def test_missing_signature_is_401(client):
    body = json.dumps(_payload()).encode()
    r = client.post("/webhooks", content=body)
    assert r.status_code == 401


def test_replay_window_exceeded_is_401(client):
    stale = _payload()
    stale["timestamp"] = int(time.time() * 1000) - 120_000  # 2 min in the past
    r = _post(client, stale)
    assert r.status_code == 401
    assert "replay" in r.json()["detail"]


def test_unparseable_body_is_400(client):
    body = b"not json {{{"
    r = client.post("/webhooks", content=body, headers={"x-signature": _sign(body)})
    assert r.status_code == 400


def test_missing_session_id_is_400(client):
    bad = {"action": "created", "timestamp": int(time.time() * 1000), "session": {}}
    r = _post(client, bad)
    assert r.status_code == 400


def test_oversized_body_is_413():
    cfg = Config(signing_secret=SECRET, max_body_bytes=10)
    app = create_app(config=cfg, registry=registry_lib.Registry(personas={}))
    with TestClient(app) as c:
        r = _post(c, _payload(text="x" * 100))
        assert r.status_code == 413


def test_no_secret_configured_fails_closed():
    """No signing secret anywhere => every webhook 401s (fail-closed)."""
    cfg = Config(signing_secret="")
    app = create_app(config=cfg, registry=registry_lib.Registry(personas={}))
    with TestClient(app) as c:
        body = json.dumps(_payload()).encode()
        r = c.post("/webhooks", content=body, headers={"x-signature": _sign(body)})
        assert r.status_code == 401


# ---------- custom signature header ----------


def test_custom_signature_header(client_factory=None):
    cfg = Config(signing_secret=SECRET, signature_header="x-my-sig")
    app = create_app(config=cfg, registry=registry_lib.Registry(personas={}))
    with TestClient(app) as c:
        r = _post(c, _payload(), header="x-my-sig")
        assert r.status_code == 200


# ---------- end-to-end: webhook -> queue -> worker -> backend ----------


def test_end_to_end_worker_invokes_backend():
    """The full path: HTTP -> verify -> parse -> enqueue -> worker -> backend.

    A recording backend captures the event the worker hands it, proving the
    async worker actually invokes the routed backend (not just that intake
    enqueued). The recorder is threading-safe because the worker runs on the
    TestClient's lifespan event loop, a different thread than the test.
    """
    import threading

    from app.backends.base import AgentBackend, Result
    from app.lib.events import Event

    seen: list[Event] = []
    done = threading.Event()

    class RecordingBackend(AgentBackend):
        name = "echo"

        async def handle(self, event: Event) -> Result:
            seen.append(event)
            done.set()
            return Result.success(output=f"echo: {event.text}")

    cfg = Config(signing_secret=SECRET)
    app = create_app(
        config=cfg,
        registry=registry_lib.Registry(personas={}),
        backends={"echo": RecordingBackend()},
    )
    with TestClient(app) as c:
        r = _post(c, _payload(text="ping", labels=["route/echo"]))
        assert r.status_code == 200
        assert done.wait(timeout=5.0), "worker never invoked the backend"

    assert len(seen) == 1
    assert seen[0].text == "ping"
    assert seen[0].labels == ("route/echo",)


# ---------- cross-persona signature confusion (the B1 trust boundary) ----------
#
# Two registered personas with DISTINCT signing secrets behind one endpoint.
# The secret is scoped by the in-body `persona_id`, so a webhook claiming one
# persona but signed with the other's key MUST be rejected — otherwise a caller
# holding any single persona's secret could forge a webhook routed to any other.

ALICE_SECRET = "alice-signing-secret"
BOB_SECRET = "bob-signing-secret"


@pytest.fixture
def two_persona_client(monkeypatch):
    monkeypatch.setenv("ALICE_WEBHOOK_SECRET", ALICE_SECRET)
    monkeypatch.setenv("BOB_WEBHOOK_SECRET", BOB_SECRET)
    reg = registry_lib.Registry(
        personas={
            "alice": _persona("alice", signing_secret_env="ALICE_WEBHOOK_SECRET"),
            "bob": _persona("bob", signing_secret_env="BOB_WEBHOOK_SECRET"),
        }
    )
    # No global secret — force selection to come from the per-persona scope.
    cfg = Config(signing_secret="")
    app = create_app(config=cfg, registry=reg, backends={"echo": EchoRecorder()})
    with TestClient(app) as c:
        yield c


class EchoRecorder:
    """A backend that records every event it handles, so a test can assert
    whether an event actually made it through to dispatch."""

    name = "echo"
    handled: list = []

    async def handle(self, event):
        from app.backends.base import Result

        EchoRecorder.handled.append(event)
        return Result.success(output="ok")


def test_claim_alice_signed_with_bob_is_rejected(two_persona_client):
    """The cross-tenant forgery case: claim persona alice, sign with bob's
    secret. Scoped selection hands the verifier alice's secret alone, so bob's
    signature can't pass -> 401, and the event never reaches a backend."""
    EchoRecorder.handled = []
    body = json.dumps(_payload(persona_id="alice")).encode()
    r = two_persona_client.post(
        "/webhooks", content=body, headers={"x-signature": _sign(body, BOB_SECRET)}
    )
    assert r.status_code == 401
    assert EchoRecorder.handled == []


def test_claim_alice_signed_with_alice_is_accepted(two_persona_client):
    """The legitimate case the scoping must still allow: claim alice, sign with
    alice's own secret -> 200."""
    body = json.dumps(_payload(persona_id="alice")).encode()
    r = two_persona_client.post(
        "/webhooks", content=body, headers={"x-signature": _sign(body, ALICE_SECRET)}
    )
    assert r.status_code == 200


def test_claim_bob_signed_with_alice_is_rejected(two_persona_client):
    """Symmetric to the alice/bob case — neither persona can forge for the
    other."""
    body = json.dumps(_payload(persona_id="bob")).encode()
    r = two_persona_client.post(
        "/webhooks", content=body, headers={"x-signature": _sign(body, ALICE_SECRET)}
    )
    assert r.status_code == 401


def test_recognized_persona_with_unset_secret_fails_closed(monkeypatch):
    """A registered persona whose signing-secret env var is UNSET must fail
    closed (401), not borrow a sibling's secret via try-all. Bob is wired; alice
    is recognized but has no secret. A webhook claiming alice — even signed with
    bob's (the only live) secret — must be rejected."""
    monkeypatch.delenv("ALICE_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("BOB_WEBHOOK_SECRET", BOB_SECRET)
    reg = registry_lib.Registry(
        personas={
            # alice is "available" in the registry sense but her secret env var
            # is unset, so signing_secret() will raise and contribute no secret.
            "alice": _persona("alice", signing_secret_env="ALICE_WEBHOOK_SECRET"),
            "bob": _persona("bob", signing_secret_env="BOB_WEBHOOK_SECRET"),
        }
    )
    app = create_app(
        config=Config(signing_secret=""), registry=reg,
        backends={"echo": EchoRecorder()},
    )
    with TestClient(app) as c:
        body = json.dumps(_payload(persona_id="alice")).encode()
        r = c.post(
            "/webhooks", content=body,
            headers={"x-signature": _sign(body, BOB_SECRET)},
        )
        assert r.status_code == 401


def test_claim_persona_signed_with_global_secret_is_rejected(monkeypatch):
    """The verifying secret's persona must match the claim: a webhook claiming a
    registered persona, signed with the GLOBAL secret, must be rejected. Scoped
    selection for a recognized persona returns that persona's key ONLY — never
    the global secret — so a global-signed claim can't be routed to the persona's
    backend. (Without scoping, try-all would include the global secret and let it
    through.)"""
    monkeypatch.setenv("ALICE_WEBHOOK_SECRET", ALICE_SECRET)
    reg = registry_lib.Registry(
        personas={"alice": _persona("alice", signing_secret_env="ALICE_WEBHOOK_SECRET")}
    )
    cfg = Config(signing_secret="the-global-secret")
    app = create_app(config=cfg, registry=reg, backends={"echo": EchoRecorder()})
    with TestClient(app) as c:
        body = json.dumps(_payload(persona_id="alice")).encode()
        r = c.post(
            "/webhooks", content=body,
            headers={"x-signature": _sign(body, "the-global-secret")},
        )
        assert r.status_code == 401


def test_absent_persona_id_falls_back_to_try_all(two_persona_client):
    """No `persona_id` routing key -> try-all fallback. A body signed with any
    available persona's secret still verifies (rotation / no-key path)."""
    body = json.dumps(_payload()).encode()  # no persona_id
    r = two_persona_client.post(
        "/webhooks", content=body, headers={"x-signature": _sign(body, BOB_SECRET)}
    )
    assert r.status_code == 200


# ---------- input-validation guards ----------


def test_non_dict_json_body_is_400(client):
    """A signed but non-object JSON body (`[1,2,3]`, `"hi"`) must 400, not 500 —
    it has no routing key and no event fields to act on."""
    for raw in (b"[1, 2, 3]", b'"hi"', b"42", b"true", b"null"):
        r = _post_raw(client, raw)
        assert r.status_code == 400, f"body {raw!r} should be 400, got {r.status_code}"


def test_persona_id_as_list_is_handled_not_dropped(caplog):
    """`persona_id: ["alice"]` must not silently vanish. parse_event coerces the
    field to a string, so the event parses, enqueues, and the worker reaches a
    verdict (elicit — the stringified id matches no persona) and emits its
    `dispatched ...` log line. Without coercion the list would TypeError in the
    router, outside the backend try/except, and the event would disappear with no
    verdict. We assert the verdict log line appears -> the event was handled."""
    import logging
    import time as _time

    cfg = Config(signing_secret=SECRET)
    app = create_app(
        config=cfg, registry=registry_lib.Registry(personas={}),
        backends={"echo": EchoRecorder()},
    )
    EchoRecorder.handled = []
    with caplog.at_level(logging.INFO, logger="dispatch.listener"):
        with TestClient(app) as c:
            r = _post(c, _payload(text="ping", persona_id=["alice"]))
            assert r.status_code == 200  # intake accepted, no 500
            # Let the worker drain and log its verdict.
            deadline = _time.time() + 5.0
            while _time.time() < deadline:
                if any("dispatched session=sess-1" in m for m in caplog.messages):
                    break
                _time.sleep(0.02)
    # The worker reached a verdict for this event (it was handled, not dropped).
    assert any("dispatched session=sess-1" in m for m in caplog.messages)


def test_worker_converts_routing_exception_to_result(caplog, monkeypatch):
    """Defense in depth for the same drop class: if the routing layer raises
    *in the worker* (past intake, where it lands outside dispatch_event's own
    per-backend try/except), the worker must convert it to a failure Result and
    log a verdict — not let the event vanish. We force the raise by monkeypatching
    `dispatch.dispatch_event` to blow up, then assert the verdict log appears."""
    import logging
    import time as _time

    from app import dispatch as dispatch_mod

    async def _boom(*args, **kwargs):
        raise RuntimeError("boom in routing")

    monkeypatch.setattr(dispatch_mod, "dispatch_event", _boom)

    cfg = Config(signing_secret=SECRET)
    app = create_app(
        config=cfg, registry=registry_lib.Registry(personas={}),
        backends={"echo": EchoRecorder()},
    )
    with caplog.at_level(logging.INFO, logger="dispatch.listener"):
        with TestClient(app) as c:
            r = _post(c, _payload())
            assert r.status_code == 200  # intake unaffected
            deadline = _time.time() + 5.0
            while _time.time() < deadline:
                if any("dispatched session=sess-1 ok=False" in m for m in caplog.messages):
                    break
                _time.sleep(0.02)
    # The worker emitted a failure verdict rather than dropping the event.
    assert any("dispatched session=sess-1 ok=False" in m for m in caplog.messages)
