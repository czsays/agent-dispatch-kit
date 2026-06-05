"""FastAPI listener — the webhook intake + async dispatch surface.

Request path (``POST /webhooks``):
    headers:
        X-Signature   hex HMAC-SHA256 of the raw body (header name configurable)
    body: JSON.
    responses:
        200 — verified, enqueued (or a no-op for an unparseable-but-signed event).
        400 — body present but missing required event fields.
        401 — missing/invalid signature, or outside the replay window.
        413 — body exceeds the configured cap.

The handler does the *minimum* on the hot path: size-guard, parse, HMAC verify,
replay-window check, enqueue, 200. All real work happens off the queue in the
worker so the ack stays fast (sub-millisecond typical) and can't be blocked by a
slow backend.

Security posture (see ``docs/adr/``):
  * HMAC verify is fail-closed: no secret configured => every webhook 401s.
  * Secret selection is *scoped* by the in-body ``persona_id`` routing key: a
    webhook claiming a persona is verified against that persona's signing
    secret only, so a payload signed with a sibling's key can't be accepted
    and routed to the claimed persona.
  * A recognized ``persona_id`` whose secret env var is unset fails closed
    (401) — it does NOT fall through to try-all and borrow a sibling's secret.
  * An absent / unrecognized routing key falls back to try-all (global +
    every available persona's secret), supporting key rotation and the
    no-routing-key case.
  * The body is parsed *before* the verify only to read the (signed) routing
    keys; no action is taken on the payload until the HMAC passes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app import dispatch
from app.backends.base import Result
from app.backends.registry import build_default_backends
from app.config import Config
from app.lib import registry as registry_lib
from app.lib import webhook_verify
from app.lib.events import EventParseError, parse_event

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("dispatch.listener")


def _persona_signing_secret(persona: registry_lib.Persona) -> str | None:
    """Resolve a persona's webhook signing secret, or None if its env var is
    unset (a misconfigured persona contributes no secret rather than raising)."""
    try:
        return persona.signing_secret()
    except RuntimeError:
        return None


def _all_secrets(config: Config, registry: registry_lib.Registry) -> list[str]:
    """The try-all candidate set: the global ``WEBHOOK_SIGNING_SECRET`` (if set)
    plus every *available* persona's signing secret. Used for boot-time
    fail-closed detection and as the fallback when an incoming webhook carries no
    (or an unrecognized) ``persona_id`` routing key. Supports key rotation and a
    single endpoint behind several signing keys. An empty result means nothing is
    configured to verify against — every webhook then fails closed (401)."""
    secrets: list[str] = []
    if config.signing_secret:
        secrets.append(config.signing_secret)
    for persona in registry.available().values():
        secret = _persona_signing_secret(persona)
        if secret:
            secrets.append(secret)
    return secrets


def _candidate_secrets(
    config: Config, registry: registry_lib.Registry, persona_id: str | None
) -> tuple[list[str], bool]:
    """Select the signing secret(s) to verify an incoming webhook against,
    scoped by the in-body ``persona_id`` routing key.

    Returns ``(secrets, scoped)``:

      * When ``persona_id`` names a *registered* persona, return just that
        persona's signing secret — the precise per-persona key (``scoped=True``).
        A recognized persona whose secret env var is unset returns ``([], True)``
        — fail closed. It does NOT fall through to try-all and borrow a
        sibling's secret, which would let some other persona's key verify a
        webhook that explicitly claims to be this (misconfigured) one and route
        it to the claimed backend.

      * When ``persona_id`` is absent or names no registered persona, fall back
        to the try-all candidate set (``scoped=False``): the global secret plus
        every available persona's secret, so a webhook from a correctly-signed
        sender still verifies even when the listener cannot pre-select. This is
        the rotation / no-routing-key path.
    """
    if persona_id:
        persona = registry.get(persona_id)
        if persona is not None:
            secret = _persona_signing_secret(persona)
            return ([secret] if secret else []), True
    return _all_secrets(config, registry), False


def create_app(
    *,
    config: Config | None = None,
    registry: registry_lib.Registry | None = None,
    backends: dict | None = None,
) -> FastAPI:
    """Build the FastAPI app. Dependencies are injectable for tests; defaults
    load from the environment / ``personas.yaml`` / the reference backends."""
    cfg = config or Config.from_env()
    backend_map = backends if backends is not None else build_default_backends()

    if registry is not None:
        reg = registry
    else:
        try:
            reg = registry_lib.load_registry()
        except registry_lib.RegistryError as exc:
            # No personas.yaml / a broken one is fine for the label-routing
            # mode — start with an empty registry and rely on label routes.
            logger.warning("registry not loaded (%s); persona routing disabled", exc)
            reg = registry_lib.Registry(personas={})

    queue: asyncio.Queue = asyncio.Queue(maxsize=cfg.queue_max)
    active: set[asyncio.Task] = set()

    async def _worker_loop() -> None:
        while True:
            try:
                event = await queue.get()
            except asyncio.CancelledError:
                return
            task = asyncio.create_task(_run(event))
            active.add(task)
            task.add_done_callback(active.discard)
            queue.task_done()

    async def _run(event) -> None:
        try:
            result = await dispatch.dispatch_event(
                event, registry=reg, backends=backend_map, config=cfg
            )
        except Exception as exc:  # noqa: BLE001 — worker must never die
            # A failure in the *routing* layer (e.g. `decide` choking on a
            # malformed field) lands outside `dispatch_event`'s own per-backend
            # try/except. Convert it to a failure Result here rather than letting
            # the event vanish — every event leaves a dispatched= log line with a
            # verdict, and a human can see it was held/failed instead of dropped.
            logger.exception("routing failed session=%s", event.session_id)
            result = Result.failure(
                output="This needs a human — routing hit an error.",
                detail=f"{type(exc).__name__}: {exc}",
            )
        logger.info(
            "dispatched session=%s ok=%s detail=%s",
            event.session_id, result.ok, result.detail,
        )

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        if not _all_secrets(cfg, reg):
            logger.warning(
                "\n"
                "==================================================================\n"
                "  No webhook signing secret configured.\n"
                "  All incoming webhooks will be rejected with 401 (fail-closed).\n"
                "  Set WEBHOOK_SIGNING_SECRET or a persona signing-secret env var.\n"
                "=================================================================="
            )
        worker = asyncio.create_task(_worker_loop())
        try:
            yield
        finally:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
            pending = list(active)
            if pending:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        asyncio.gather(*pending, return_exceptions=True), timeout=30.0
                    )

    app = FastAPI(title="agent-dispatch-kit", lifespan=lifespan)
    # Stash for tests / introspection.
    app.state.config = cfg
    app.state.registry = reg
    app.state.backends = backend_map
    app.state.queue = queue

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True, "queue_depth": queue.qsize()}

    @app.post("/webhooks")
    async def webhook_handler(request: Request) -> JSONResponse:
        headers = {k.lower(): v for k, v in request.headers.items()}
        sig = headers.get(cfg.signature_header, "")

        declared = headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > cfg.max_body_bytes:
            raise HTTPException(status_code=413, detail="body too large")

        raw = await request.body()
        if len(raw) > cfg.max_body_bytes:
            raise HTTPException(status_code=413, detail="body too large")

        # Parse-before-verify is intentional: routing keys (persona_id) live
        # inside the *signed* body, and no action is taken until the verify
        # below passes. Parsing is inert. (ADR-0001.)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("webhook: unparseable body")
            raise HTTPException(status_code=400, detail="unparseable body")

        # A non-object top-level JSON value (a list, a bare string/number) has no
        # routing key and no event fields — reject before any `.get(...)` access
        # that would 500 on it.
        if not isinstance(payload, dict):
            logger.warning("webhook: body is not a JSON object (%s)", type(payload).__name__)
            raise HTTPException(status_code=400, detail="body must be a JSON object")

        # Scope the signing secret by the in-body persona_id routing key, then
        # verify against *only* that scope. A webhook claiming persona A but
        # signed with persona B's secret fails here: scoped selection hands the
        # verifier A's secret alone, so B's signature can't pass — no
        # cross-persona confusion. (See `_candidate_secrets`; the tampered
        # persona_id is inside the signed body, so it can only ever select the
        # wrong secret and fail the verify.)
        claimed_persona_id = payload.get("persona_id")
        if claimed_persona_id is not None:
            claimed_persona_id = str(claimed_persona_id)
        secrets, scoped = _candidate_secrets(cfg, reg, claimed_persona_id)
        if not webhook_verify.verify_any(raw, sig, secrets):
            logger.warning(
                "webhook: bad signature (persona_id=%s scoped=%s candidate_secrets=%d)",
                claimed_persona_id, scoped, len(secrets),
            )
            raise HTTPException(status_code=401, detail="invalid signature")

        timestamp = payload.get("timestamp")
        if not webhook_verify.within_replay_window(
            timestamp, cfg.replay_window_seconds
        ):
            logger.warning("webhook: outside replay window ts=%s", timestamp)
            raise HTTPException(status_code=401, detail="replay window exceeded")

        try:
            event = parse_event(payload)
        except EventParseError as exc:
            logger.warning("webhook: parse failed: %s", exc)
            raise HTTPException(status_code=400, detail="missing event fields")

        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            # Deliberate backpressure: when the in-memory queue is saturated we
            # reject with 503 rather than silently dropping the event (or
            # blocking the ack). A sender that retries on 5xx (most webhook
            # providers do) then redelivers once the worker drains — so a burst
            # is shed back to the source, not lost here. The trade-off vs. a
            # 200-and-drop is explicit: prefer a retriable failure over silent
            # loss. Tune `DISPATCH_QUEUE_MAX` for the expected burst.
            logger.error("queue full, rejecting (503) session=%s", event.session_id)
            raise HTTPException(status_code=503, detail="queue full")

        return JSONResponse({"ok": True, "queued": True})

    return app


# Module-level app for `uvicorn app.listener:app` and `python -c "import app.listener"`.
app = create_app()
