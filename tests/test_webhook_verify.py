"""Tests for app.lib.webhook_verify — the HMAC trust boundary."""

from __future__ import annotations

import hashlib
import hmac
import time

from app.lib import webhook_verify


def _sig(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ---------- verify ----------


def test_verify_accepts_valid_signature():
    secret = "shh-its-a-secret"
    body = b'{"foo": "bar"}'
    assert webhook_verify.verify(body, _sig(secret, body), secret) is True


def test_sign_matches_verify():
    secret = "rotating-key"
    body = b'{"x": 1}'
    assert webhook_verify.verify(body, webhook_verify.sign(body, secret), secret)


def test_verify_rejects_tampered_body():
    secret = "shh-its-a-secret"
    body = b'{"foo": "bar"}'
    sig = _sig(secret, body)
    assert webhook_verify.verify(b'{"foo": "BAZ"}', sig, secret) is False


def test_verify_rejects_wrong_secret():
    body = b'{"foo": "bar"}'
    assert webhook_verify.verify(body, _sig("right", body), "wrong") is False


def test_verify_rejects_empty_signature():
    body = b'{"foo": "bar"}'
    assert webhook_verify.verify(body, "", "secret") is False


def test_verify_rejects_empty_secret():
    """An unset secret must NEVER pass — otherwise an attacker computes HMAC
    with secret='' and the receiver accepts it."""
    body = b'{"foo": "bar"}'
    assert webhook_verify.verify(body, _sig("", body), "") is False


def test_verify_rejects_empty_body():
    assert webhook_verify.verify(b"", _sig("s", b""), "s") is False


def test_verify_strips_signature_whitespace():
    secret, body = "s", b'{"x": 1}'
    sig = _sig(secret, body)
    assert webhook_verify.verify(body, f"  {sig}  ", secret) is True


def test_verify_uses_constant_time_compare(monkeypatch):
    """Sanity-check the impl uses hmac.compare_digest, not ==. A timing attack
    on `==` would let an attacker forge a signature prefix-by-prefix."""
    called = {"n": 0}
    real = hmac.compare_digest

    def spy(a, b):
        called["n"] += 1
        return real(a, b)

    monkeypatch.setattr(webhook_verify.hmac, "compare_digest", spy)
    secret, body = "s", b"hello"
    webhook_verify.verify(body, _sig(secret, body), secret)
    assert called["n"] >= 1


# ---------- verify_any (rotation / multi-tenant) ----------


def test_verify_any_true_when_one_secret_matches():
    body = b'{"x": 1}'
    sig = _sig("right", body)
    assert webhook_verify.verify_any(body, sig, ["wrong-1", "right", "wrong-2"]) is True


def test_verify_any_false_when_no_secret_matches():
    body = b'{"x": 1}'
    sig = _sig("right", body)
    assert webhook_verify.verify_any(body, sig, ["wrong-1", "wrong-2"]) is False


def test_verify_any_empty_secrets_is_fail_closed():
    body = b'{"x": 1}'
    assert webhook_verify.verify_any(body, _sig("s", body), []) is False


def test_verify_any_skips_empty_secrets():
    body = b'{"x": 1}'
    sig = _sig("real", body)
    assert webhook_verify.verify_any(body, sig, ["", "real"]) is True
    assert webhook_verify.verify_any(body, sig, ["", ""]) is False


# ---------- within_replay_window ----------


def test_replay_window_accepts_recent_timestamp():
    now = int(time.time() * 1000)
    assert webhook_verify.within_replay_window(now, 60, now_ms=now) is True


def test_replay_window_accepts_30s_skew():
    now = int(time.time() * 1000)
    assert webhook_verify.within_replay_window(now - 30_000, 60, now_ms=now) is True


def test_replay_window_rejects_61s_skew():
    now = int(time.time() * 1000)
    assert webhook_verify.within_replay_window(now - 61_000, 60, now_ms=now) is False


def test_replay_window_rejects_future_skew():
    """Symmetric — a far-future timestamp must also be rejected, or an attacker
    parks `now + 1 year` and replays later."""
    now = int(time.time() * 1000)
    assert webhook_verify.within_replay_window(now + 61_000, 60, now_ms=now) is False


def test_replay_window_rejects_missing_timestamp():
    now = int(time.time() * 1000)
    assert webhook_verify.within_replay_window(None, 60, now_ms=now) is False


def test_replay_window_rejects_string_timestamp():
    assert webhook_verify.within_replay_window("1715000000000", 60) is False
