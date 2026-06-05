# agent-dispatch-kit

A small, runnable reference for a **webhook-driven agent dispatch service**:
verify a signed webhook, normalize it into an event, route that event to a
pluggable *agent backend*, and run it off an async queue. It runs end-to-end out
of the box with a dependency-free echo backend — then you swap in your own.

The pattern was extracted from a private production service that fans Linear
agent-session webhooks out to several AI agents. This repo keeps the reusable
core — signed intake, the event/queue model, the commit-safe persona registry,
the pluggable backend seam — and drops everything workspace-specific.

## What's in the box

- **Signed webhook intake** — HMAC-SHA256 verification of the raw body, with a
  symmetric replay-window check and fail-closed on a missing secret. The signing
  secret is scoped by the in-body `persona_id` so a webhook claiming one persona
  but signed with another's key is rejected; an absent/unrecognized key falls
  back to try-all for key rotation.
- **An async event queue** — the HTTP handler verifies + enqueues + acks fast; a
  worker drains the queue so a slow backend never blocks intake. When the queue
  is saturated the handler returns **503** (deliberate backpressure) rather than
  dropping the event — a sender that retries on 5xx redelivers once the worker
  drains, so a burst is shed back to the source instead of lost. Size it with
  `DISPATCH_QUEUE_MAX`.
- **A pluggable backend seam** — `AgentBackend` is a one-method
  `async def handle(event) -> Result` Protocol. Ships with `EchoBackend` and
  `LogBackend` so the app works immediately. Bring your own.
- **A commit-safe persona registry** — `personas.yaml` references secrets by
  env-var *name*, never value, so it's safe to commit. Two-tier fail-closed
  loading: structural errors raise; a persona with an unset secret loads
  `available=False` but keeps its identity so routing still resolves.
- **A prompt-injection envelope** — untrusted user text is wrapped in a fenced
  `<user_request>` envelope + directive before any LLM backend sees it.

## Architecture

```
                  POST /webhooks
                        │
            ┌───────────▼────────────┐
            │  listener (FastAPI)     │   hot path, sub-ms:
            │  1. size guard (413)    │   verify + enqueue + 200
            │  2. parse JSON (400)    │
            │  3. HMAC verify (401)   │◄── secret scoped by persona_id:
            │  4. replay window (401) │     that persona's key, else
            │  5. normalize -> Event  │     try-all (rotation / no key)
            │  6. enqueue ──────────┐ │
            └───────────────────────┼─┘
                                    │  asyncio.Queue
            ┌───────────────────────▼─┐
            │  worker loop             │   off the hot path
            │   dispatch.decide(event) │   ── routing decision (pure) ──┐
            └──────────────────────────┘                                │
                                                                        ▼
        persona binding ─┐         route/<label> ─┐        no route ─┐
        (registry)       │         (config table) │                 │
                         ▼                         ▼                 ▼
                   AgentBackend.handle(event) -> Result          ELICIT
                   (echo / log / your own)                  (hold for a human)
```

Routing precedence: a bound **persona** (if available) wins; else a single
recognized **`route/<name>` label**; else **elicit** (hold for a human).

## Quickstart

```bash
# 1. Create a venv and install runtime + dev deps.
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# 2. Run the tests (all green).
pytest -q

# 3. Configure and run the service.
cp .env.example .env            # set WEBHOOK_SIGNING_SECRET to a random string
export WEBHOOK_SIGNING_SECRET=dev-secret
uvicorn app.listener:app --reload --port 8000
```

Send a signed test webhook (the signature is HMAC-SHA256 of the raw body):

```bash
SECRET=dev-secret
BODY=$(python3 - <<'PY'
import json, time
print(json.dumps({
    "action": "created",
    "timestamp": int(time.time() * 1000),
    "labels": ["route/echo"],
    "session": {"id": "sess-1", "issue_id": "iss-1", "text": "hello there"},
}))
PY
)
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | sed 's/^.* //')
curl -s -X POST localhost:8000/webhooks \
  -H "x-signature: $SIG" -H 'content-type: application/json' \
  -d "$BODY"
# -> {"ok": true, "queued": true}
# The echo backend logs:  echo: hello there
```

`GET /healthz` returns `{"ok": true, "queue_depth": N}`.

## Bring your own backend

```python
# app/backends/my_agent.py
from app.backends.base import AgentBackend, Result
from app.lib.events import Event
from app.lib.prompt_safety import wrap_user_content

class MyAgentBackend(AgentBackend):
    name = "my-agent"

    async def handle(self, event: Event) -> Result:
        prompt = wrap_user_content(event.text)   # fence untrusted input
        # ... call your LLM / spawn your subprocess / relay it ...
        return Result.success(output="...done...")
```

Register it in `app/backends/registry.py` (`build_default_backends`), then point
a persona's `backend:` field or a `route/<name>` label at `"my-agent"`. No
change to the dispatcher or the listener.

## Configuration

| Env var | Default | Purpose |
| --- | --- | --- |
| `WEBHOOK_SIGNING_SECRET` | *(unset)* | Global HMAC signing secret. **Unset => every webhook 401s** (fail-closed). |
| `WEBHOOK_SIGNATURE_HEADER` | `x-signature` | Header carrying the hex HMAC-SHA256 of the raw body. |
| `WEBHOOK_REPLAY_WINDOW_SECONDS` | `60` | Symmetric ± replay-window half-width. |
| `WEBHOOK_MAX_BODY_BYTES` | `1048576` | Max body bytes before a 413. |
| `WEBHOOK_LABEL_ROUTES` | `echo:echo,log:log` | `route/<name>` → backend map, as comma-separated `name:backend` pairs. |
| `DISPATCH_QUEUE_MAX` | `128` | In-memory queue depth between handler and worker (saturated → 503). |
| `LOG_LEVEL` | `INFO` | Standard logging level. |
| `<PERSONA>_API_TOKEN` / `<PERSONA>_WEBHOOK_SECRET` | *(unset)* | Per-persona secrets, referenced by name in `personas.yaml`. |

See `.env.example` for the full list. Per-persona env-var **names** live in
`personas.yaml`; the **values** live only in your environment.

## Docker

```bash
docker build -t agent-dispatch .
docker run --rm -p 8000:8000 --env-file .env agent-dispatch
```

## Design records

Three sanitized ADRs in [`docs/adr/`](docs/adr/) carry the security reasoning
behind the intake and execution boundaries:

- [0001 — Webhook signature verification](docs/adr/0001-webhook-signature-verification.md)
- [0002 — Prompt-injection delimiters for untrusted user content](docs/adr/0002-prompt-injection-delimiters.md)
- [0003 — Commit-safe persona registry](docs/adr/0003-commit-safe-persona-registry.md)
- [0004 — Pluggable-backend routing](docs/adr/0004-pluggable-backend-routing.md)

## License

MIT — see [LICENSE](LICENSE).
