"""Tests for app.lib.registry — the commit-safe persona registry.

Pins the two-tier fail-closed contract: structural failures raise; per-persona
failures never raise and never touch siblings.
"""

from __future__ import annotations

import textwrap

import pytest

from app.lib import registry as registry_lib


def _write(tmp_path, text: str):
    p = tmp_path / "personas.yaml"
    p.write_text(textwrap.dedent(text))
    return p


# ---------- structural failures raise ----------


def test_missing_file_raises(tmp_path):
    with pytest.raises(registry_lib.RegistryError, match="not found"):
        registry_lib.load_registry(tmp_path / "nope.yaml")


def test_bad_yaml_raises(tmp_path):
    p = _write(tmp_path, "personas: [unclosed")
    with pytest.raises(registry_lib.RegistryError, match="not valid YAML"):
        registry_lib.load_registry(p)


def test_no_personas_block_raises(tmp_path):
    p = _write(tmp_path, "other: stuff\n")
    with pytest.raises(registry_lib.RegistryError, match="personas"):
        registry_lib.load_registry(p)


# ---------- per-persona: available when env vars are set ----------

_ONE = """
personas:
  responder:
    display_name: Responder
    role: worker
    backend: echo
    token_env: TOK
    signing_secret_env: SIG
"""


def test_persona_available_when_secrets_present(tmp_path):
    p = _write(tmp_path, _ONE)
    reg = registry_lib.load_registry(p, env={"TOK": "t", "SIG": "s"})
    persona = reg.get("responder")
    assert persona is not None
    assert persona.available is True
    assert persona.backend == "echo"
    assert persona.token({"TOK": "t", "SIG": "s"}) == "t"
    assert persona.signing_secret({"TOK": "t", "SIG": "s"}) == "s"
    assert reg.available() == {"responder": persona}


def test_persona_unavailable_but_identity_intact_when_secret_unset(tmp_path):
    """Structurally valid + operability failure: keeps role/backend, available=False."""
    p = _write(tmp_path, _ONE)
    reg = registry_lib.load_registry(p, env={"TOK": "t"})  # SIG unset
    persona = reg.get("responder")
    assert persona.available is False
    assert "SIG" in persona.unavailable_reason
    assert persona.backend == "echo"  # identity preserved
    assert reg.available() == {}


# ---------- per-persona: malformed entry -> zeroed stub, no raise ----------


def test_malformed_entry_does_not_raise_and_is_scoped(tmp_path):
    p = _write(
        tmp_path,
        """
        personas:
          good:
            display_name: Good
            role: worker
            backend: echo
            token_env: TOK
            signing_secret_env: SIG
          broken:
            role: worker  # missing display_name AND backend
        """,
    )
    reg = registry_lib.load_registry(p, env={"TOK": "t", "SIG": "s"})
    assert reg.get("good").available is True  # sibling unaffected
    broken = reg.get("broken")
    assert broken.available is False
    assert "display_name" in broken.unavailable_reason


def test_invalid_role_is_unavailable(tmp_path):
    p = _write(
        tmp_path,
        """
        personas:
          weird:
            display_name: Weird
            role: wizard
            backend: echo
            token_env: TOK
            signing_secret_env: SIG
        """,
    )
    reg = registry_lib.load_registry(p, env={"TOK": "t", "SIG": "s"})
    assert reg.get("weird").available is False


def test_token_resolution_raises_when_unset():
    persona = registry_lib.Persona(
        slug="x", display_name="X", role="worker", backend="echo",
        token_env="TOK", signing_secret_env="SIG", capabilities=(),
        available=True, unavailable_reason=None,
    )
    with pytest.raises(RuntimeError, match="TOK"):
        persona.token({})
