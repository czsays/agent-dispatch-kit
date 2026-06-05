"""Tests for app.config — env-sourced runtime configuration.

Pins WEBHOOK_LABEL_ROUTES parsing (the route/<name> -> backend table is now
env-driven, not a hardcoded default) and the from_env defaults.
"""

from __future__ import annotations

from app.config import Config, _parse_label_routes


# ---------- _parse_label_routes ----------


def test_label_routes_default_when_unset():
    assert _parse_label_routes(None) == {"echo": "echo", "log": "log"}
    assert _parse_label_routes("") == {"echo": "echo", "log": "log"}
    assert _parse_label_routes("   ") == {"echo": "echo", "log": "log"}


def test_label_routes_parses_pairs():
    assert _parse_label_routes("triage:my-agent") == {"triage": "my-agent"}
    assert _parse_label_routes("a:x,b:y") == {"a": "x", "b": "y"}


def test_label_routes_tolerates_whitespace():
    assert _parse_label_routes(" a : x , b : y ") == {"a": "x", "b": "y"}


def test_label_routes_skips_malformed_pairs():
    # Missing colon, empty name, empty backend -> skipped, others survive.
    assert _parse_label_routes("good:be,nobackend:,:noname,bare") == {"good": "be"}


def test_label_routes_all_malformed_falls_back_to_default():
    assert _parse_label_routes("bare,:,") == {"echo": "echo", "log": "log"}


# ---------- from_env ----------


def test_from_env_defaults():
    cfg = Config.from_env(env={})
    assert cfg.signature_header == "x-signature"
    assert cfg.signing_secret == ""
    assert cfg.replay_window_seconds == 60
    assert cfg.label_routes == {"echo": "echo", "log": "log"}


def test_from_env_reads_label_routes():
    cfg = Config.from_env(env={"WEBHOOK_LABEL_ROUTES": "triage:my-agent,log:log"})
    assert cfg.label_routes == {"triage": "my-agent", "log": "log"}


def test_from_env_lowercases_signature_header():
    cfg = Config.from_env(env={"WEBHOOK_SIGNATURE_HEADER": "X-My-Sig"})
    assert cfg.signature_header == "x-my-sig"
