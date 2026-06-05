"""HMAC-SHA256 signature verification + replay-window check for inbound webhooks.

This is provider-agnostic. The canonical webhook contract assumed here:

    Header: X-Signature   hex-encoded HMAC-SHA256 of the *raw* request body
    Body:   JSON, with an integer `timestamp` field (ms since epoch) used for
            the replay-window check.

The signature *header* name is configurable at the call site (``Config.
signature_header``). The replay timestamp is read by the listener from the
hardcoded top-level ``timestamp`` field and passed to ``within_replay_window``
as a value, not a field name — adapt ``app.listener`` if your provider names it
differently. The verification primitives below are pure and take the raw bytes +
signature string (and the timestamp value) directly, so they are trivial to
unit-test and to wire to any provider that signs the raw body with a shared
secret.

Design notes (see docs/adr/0001-webhook-signature-verification.md):
  * Constant-time compare (``hmac.compare_digest``) — never ``==`` — so a
    timing side-channel can't be used to forge a signature prefix-by-prefix.
  * Empty secret / empty signature / empty body all fail closed. An unset
    signing secret must NEVER verify (otherwise an attacker computes HMAC with
    ``secret=""`` and the receiver accepts it).
  * Multi-secret verify (``verify_any``) supports key rotation and multi-tenant
    fan-out: one endpoint behind several signing keys. Fail-closed on an empty
    candidate set.
  * Symmetric replay window via ``abs()`` so both past- and future-skewed
    timestamps are bounded — a far-future timestamp can't be parked and replayed.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Sequence


def sign(raw_body: bytes, secret: str) -> str:
    """Compute the hex HMAC-SHA256 a sender would attach for ``raw_body``.

    Exposed for tests and for clients that need to *produce* a signature
    (e.g. a relay re-signing a payload). The receiver never calls this.
    """
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def verify(raw_body: bytes, signature_header: str, secret: str) -> bool:
    """Constant-time HMAC-SHA256 verify of a raw webhook body.

    Returns ``False`` (fail-closed) for an empty secret, empty signature, or
    empty body — none of those should ever pass.
    """
    if not secret or not signature_header or not raw_body:
        return False
    expected = sign(raw_body, secret)
    return hmac.compare_digest(expected, signature_header.strip())


def verify_any(
    raw_body: bytes, signature_header: str, secrets: Sequence[str]
) -> bool:
    """True if *any* candidate secret verifies the body.

    Supports key rotation (old + new secret valid during a window) and a single
    endpoint fronting multiple signing keys. Each attempt routes through
    ``verify``, so the constant-time compare and empty-secret/empty-body guards
    hold per secret. An empty ``secrets`` sequence returns ``False`` —
    fail-closed, identical to an unset secret.
    """
    return any(verify(raw_body, signature_header, s) for s in secrets)


def within_replay_window(
    timestamp_ms: int | float | None,
    window_seconds: int,
    *,
    now_ms: int | None = None,
) -> bool:
    """Reject a payload whose ``timestamp_ms`` is more than ``window_seconds``
    away from ``now`` in *either* direction (symmetric ±window via ``abs()``).

    A missing or non-numeric timestamp fails closed. ``now_ms`` is injectable
    for deterministic tests.
    """
    if not isinstance(timestamp_ms, (int, float)):
        return False
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    return abs(now - int(timestamp_ms)) <= window_seconds * 1000
