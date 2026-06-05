# ADR-0003: Commit-safe persona registry — env-var-name references, two-tier fail-closed loading

Date: 2026-06-05
Status: Accepted

## Context

The service routes events to multiple agent identities ("personas"): each has
its own API token, its own webhook signing secret, and its own backend. Two
things were tangled together in the first cut: the routing table (which persona
owns which work) was hardcoded in the dispatcher, and the secrets were read from
a single global env var. Adding a persona meant a code change, and the config
that *described* personas couldn't be committed without either leaking secrets or
omitting the secrets and becoming useless.

We want a single declarative file that (a) is safe to commit to a public repo,
(b) lets the Nth persona be added with a config entry and some env vars — zero
code change — and (c) degrades safely when a persona is half-configured (its
OAuth app exists but its secret isn't wired up yet, say).

## Decision

A `personas.yaml` registry, loaded + validated once at startup, where every
secret is referenced by **env-var name, never value**:

```yaml
personas:
  responder:
    display_name: Responder
    role: worker
    backend: echo                           # which registered backend handles it
    token_env: RESPONDER_API_TOKEN          # the NAME, not the token
    signing_secret_env: RESPONDER_WEBHOOK_SECRET
```

The loader holds only the names; resolver methods (`persona.token()`,
`persona.signing_secret()`) read the live environment on demand, so no secret
value is ever stored on the (frozen, long-lived) `Persona` object. The file is
committable; the secrets live in `.env` / a vault.

**Two-tier fail-closed loading.** Failures are deliberately split:

- **Structural failure** — file missing, unparseable YAML, no `personas:`
  mapping — means the registry itself is broken. `load_registry` *raises*. Fail
  loud; the service should not boot half-blind to who exists.

- **Per-persona failure never raises and never affects siblings:**
  - *Malformed entry* (not a mapping, missing `display_name`, invalid `role`,
    missing an env-ref field name) → a zeroed stub, `available=False` — only the
    slug is registered. The structure can't be trusted, so nothing else is kept.
  - *Structurally valid but not operable* (a referenced secret env var is unset)
    → a **fully-populated** `Persona`, `available=False`, with its identity
    (role, backend) intact. The entry is well-formed; it just can't be dispatched
    to *yet*.

The distinction matters for routing: a persona's *structural* fields (role,
backend) survive an operability failure, and the dispatcher reads them directly
off the registry by `persona_id`. So an event bound for a not-yet-wired persona
still resolves to "this is persona X, which is unavailable — hold for a human"
rather than silently misrouting or crashing. Work bound for any `available=False`
persona routes to the elicit / hold-for-a-human path.

Note the registry is **not** a label-routing table. An earlier cut had personas
carry a `labels:` field and a `Registry.for_label` lookup, but nothing read
them; that field has been removed. Routing-by-persona (this ADR) and
routing-by-`route/<name>`-label (env-driven, `WEBHOOK_LABEL_ROUTES`) are two
distinct inputs to the dispatcher — see ADR-0004 for the precedence and the
backend seam.

This composes with the broader least-privilege posture: the registry is the only
place secret names live, secret *values* never enter the committed artifact or a
spawned subprocess's environment, and a misconfigured persona contributes no
secret to the verifier rather than a blank one (a blank secret must never
verify — see ADR-0001).

## Consequences

- **+** The config file is safe to commit; the public artifact carries zero
  secret material.
- **+** Adding a persona is a YAML entry + env vars — no dispatcher change.
  Persona routing reads the registry directly by `persona_id`; there is no
  separate hand-maintained persona-routing table to keep in sync.
- **+** A half-configured persona degrades to "registered but unavailable" with a
  human-readable reason, instead of a boot crash or a silent misroute.
- **−** The split between "malformed → stub" and "not operable → full identity"
  is subtle; it needs tests pinning which fields survive which failure, or a
  refactor can quietly collapse the two tiers and lose the property that an
  unavailable persona still resolves to a held-for-a-human verdict rather than a
  crash or a misroute.
- **−** Secrets-by-reference means a deploy that forgets to set an env var fails
  *quietly* (persona unavailable) rather than loudly. Mitigated by a startup
  warning log per unavailable persona.

## Alternatives considered

- **Hardcoded routing map + single global token.** What we had. Doesn't scale
  past one identity and can't be made commit-safe without losing the secrets.
- **Inline secret values in the config (gitignored file).** Then the file can't
  be committed at all, so the *shape* of the config (who exists, who routes
  where) isn't reviewable in the repo. Names-not-values keeps the shape public
  and the values private.
- **A secrets manager / KMS lookup at load.** Stronger for value storage, but
  heavier and orthogonal — the registry still only needs to know *which* secret
  to fetch, i.e. a name. The env-var indirection is the minimal version of the
  same idea and swaps cleanly for a KMS resolver later.
