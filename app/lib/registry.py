"""Persona registry — loads + validates ``personas.yaml``.

The registry is the *commit-safe* config seam: it holds references to secrets
(env-var **names**), never secret values. The values live in the process
environment, keyed by the ``*_env`` names declared per persona. This lets the
YAML be committed to a public repo while the secrets stay in ``.env`` / a vault.

Two-tier fail-closed design (see
``docs/adr/0003-commit-safe-persona-registry.md``):

  * **Structural** failure — file missing, unparseable YAML, no ``personas:``
    mapping — means the registry itself is broken. ``load_registry`` raises.
    Fail loud; the service should not boot half-blind.

  * **Per-persona** failure never raises and never affects siblings. It splits:
      - *Malformed entry* (not a mapping, missing ``display_name``, invalid
        ``role``, a missing env-ref field name) → a zeroed stub,
        ``available=False`` — only the slug is registered.
      - *Structurally valid but not operable* (a referenced secret env var is
        unset) → a fully-populated ``Persona`` with ``available=False`` and its
        structural identity intact, so callers still know *who* it is and can
        route work bound for it to a human / hold state.

Per-persona schema (``personas.yaml``):
    display_name        human-facing nickname (mutable; nothing keys on it)
    role                "orchestrator" | "worker"
    backend             which registered backend handles this persona's events
    token_env           env-var NAME holding this persona's API token
    signing_secret_env  env-var NAME holding this persona's webhook signing secret
    capabilities        free-form descriptive list (optional)

Routing is by ``persona_id`` (the in-body routing key selects the persona, which
selects the backend) or by a ``route/<name>`` label mapped to a backend via
``WEBHOOK_LABEL_ROUTES`` (see ``app.config`` and ADR-0003). Personas do not
claim labels — label routing maps a label straight to a backend, not via a
persona.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml

logger = logging.getLogger("dispatch.registry")

_DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parent.parent.parent / "personas.yaml"

VALID_ROLES: frozenset[str] = frozenset({"orchestrator", "worker"})

# Env-var-name reference fields every persona must declare. The *values* live in
# the process env; the registry only ever holds the names (commit-safe).
_REQUIRED_ENV_REF_FIELDS: tuple[str, ...] = ("token_env", "signing_secret_env")


class RegistryError(RuntimeError):
    """Structural failure loading the registry — the file itself is broken."""


@dataclass(frozen=True)
class Persona:
    """One registry entry, keyed on ``slug``.

    The ``*_env`` fields are env-var *names*; the resolver methods read the live
    env on demand so no secret value is held on this (frozen, long-lived) object.
    """

    slug: str
    display_name: str
    role: str  # "orchestrator" | "worker"
    backend: str
    token_env: str
    signing_secret_env: str
    capabilities: tuple[str, ...]
    available: bool
    unavailable_reason: str | None

    @property
    def is_orchestrator(self) -> bool:
        return self.role == "orchestrator"

    def token(self, env: Mapping[str, str] | None = None) -> str:
        """Resolve this persona's API token from the live env. Raises if unset —
        callers should only reach here for an ``available`` persona."""
        return self._resolve_secret(self.token_env, env)

    def signing_secret(self, env: Mapping[str, str] | None = None) -> str:
        """Resolve this persona's webhook signing secret from the live env."""
        return self._resolve_secret(self.signing_secret_env, env)

    def _resolve_secret(self, env_name: str, env: Mapping[str, str] | None) -> str:
        source = os.environ if env is None else env
        value = source.get(env_name)
        if not value:
            raise RuntimeError(
                f"persona {self.slug!r}: env var {env_name!r} is not set"
            )
        return value


@dataclass(frozen=True)
class Registry:
    """The loaded persona set, keyed by ``slug``."""

    personas: dict[str, Persona]

    def get(self, slug: str | None) -> Persona | None:
        if slug is None:
            return None
        return self.personas.get(slug)

    def available(self) -> dict[str, Persona]:
        """Personas that passed the fail-closed checks at load time."""
        return {k: v for k, v in self.personas.items() if v.available}


def load_registry(
    path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Registry:
    """Load + validate ``personas.yaml``.

    Raises :class:`RegistryError` on a structural problem (missing file, bad
    YAML, no ``personas:`` mapping). Per-persona problems never raise — the
    persona loads ``available=False`` with a reason.
    """
    registry_path = Path(path) if path is not None else _DEFAULT_REGISTRY_PATH
    resolved_env = os.environ if env is None else env

    if not registry_path.exists():
        raise RegistryError(f"personas.yaml not found at {registry_path}")

    try:
        raw = yaml.safe_load(registry_path.read_text())
    except yaml.YAMLError as exc:
        raise RegistryError(f"personas.yaml is not valid YAML: {exc}") from exc

    if not isinstance(raw, Mapping):
        raise RegistryError("personas.yaml top level must be a mapping")

    personas_block = raw.get("personas")
    if not isinstance(personas_block, Mapping) or not personas_block:
        raise RegistryError(
            "personas.yaml must contain a non-empty `personas:` mapping"
        )

    personas: dict[str, Persona] = {}
    for slug, entry in personas_block.items():
        personas[str(slug)] = _build_persona(str(slug), entry, resolved_env)

    return Registry(personas=personas)


def _build_persona(slug: str, entry: object, env: Mapping[str, str]) -> Persona:
    """Build one ``Persona``. Never raises — a failure is always scoped to this
    one persona, landing in one of two tiers (see module docstring)."""
    if not isinstance(entry, Mapping):
        return _unavailable_stub(slug, "registry entry is not a mapping")

    display_name = entry.get("display_name")
    if not display_name or not isinstance(display_name, str):
        return _unavailable_stub(slug, "missing required field: display_name")

    role = entry.get("role")
    if role not in VALID_ROLES:
        return _unavailable_stub(
            slug, f"invalid role {role!r} (expected one of {sorted(VALID_ROLES)})"
        )

    backend = entry.get("backend")
    if not backend or not isinstance(backend, str):
        return _unavailable_stub(slug, "missing required field: backend")

    env_refs: dict[str, str] = {}
    for field_name in _REQUIRED_ENV_REF_FIELDS:
        ref = entry.get(field_name)
        if not ref or not isinstance(ref, str):
            return _unavailable_stub(slug, f"missing required field: {field_name}")
        env_refs[field_name] = ref

    capabilities = tuple(
        str(c) for c in (entry.get("capabilities") or []) if c is not None
    )

    # Structural validation passed. Any failure from here is an *operability*
    # problem — the persona keeps its full identity but loads available=False.
    unavailable_reason: str | None = None
    for field_name in _REQUIRED_ENV_REF_FIELDS:
        if not env.get(env_refs[field_name]):
            unavailable_reason = (
                f"env var {env_refs[field_name]!r} (from {field_name}) is not set"
            )
            break

    if unavailable_reason is not None:
        logger.warning("persona %r is unavailable: %s", slug, unavailable_reason)

    return Persona(
        slug=slug,
        display_name=display_name,
        role=role,
        backend=backend,
        token_env=env_refs["token_env"],
        signing_secret_env=env_refs["signing_secret_env"],
        capabilities=capabilities,
        available=unavailable_reason is None,
        unavailable_reason=unavailable_reason,
    )


def _unavailable_stub(slug: str, reason: str) -> Persona:
    """A minimally-populated, ``available=False`` persona. The pipeline can still
    see that the slug is registered without trusting any of its config."""
    logger.warning("persona %r is unavailable: %s", slug, reason)
    return Persona(
        slug=slug,
        display_name=slug,
        role="worker",
        backend="",
        token_env="",
        signing_secret_env="",
        capabilities=(),
        available=False,
        unavailable_reason=reason,
    )
