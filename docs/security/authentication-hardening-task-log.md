# HTTP Authentication Hardening Task Log

## Scope

This log covers only the first audit task: hardening the LandOS Engine HTTP
authentication boundary. It excludes AXON contracts, tenant/RBAC redesign,
database or migration work, deployment, tax-sale promotion enforcement, and
autonomous-write authorization.

## Evidence reviewed before editing

- Audit artifacts `00_EXECUTIVE_FINDINGS.md`, `04_CONTRADICTIONS.md`,
  `06_API_CONTRACTS.md`, `08_SECURITY_BOUNDARIES.md`,
  `09_IMPLEMENTATION_SEQUENCE.md`, and `10_MISSING_EVIDENCE.md`.
- Executable API entrypoint, all 17 declared routes, generated FastAPI
  documentation routes, authentication helpers, configuration validation,
  database-pool construction, deployment files, CLI entrypoints, tests, and
  tax-sale mint gates in the current worktree.

## Verified starting state

- `/health` is public but queries the database and exposes dependency state.
- Six sensitive routes fail open when `NVE_LAND_TOKEN` is absent.
- Ten capability routes use one of three inconsistent shared-secret guards.
- Swagger UI, ReDoc, and OpenAPI are public through FastAPI defaults.
- `LANDOS_SECRET` is already the mandatory canonical service credential.
- Deployment code and documentation target public-HTTPS service-to-service;
  live deployment state remains unverified. CLI and batch workflows invoke
  Python directly and sit outside the HTTP boundary.

## Implementation plan

1. Centralize strict bearer parsing, legacy `X-LANDOS-SECRET` compatibility,
   constant-time comparison, minimal `401` errors, and `WWW-Authenticate`.
2. Keep `LANDOS_SECRET` mandatory; validate optional scoped compatibility
   tokens at construction and never treat their absence as public access.
3. Apply the centralized guard to every capability route; retain `/health` as
   the sole public route and reduce it to dependency-free liveness.
4. Disable OpenAPI, Swagger UI, and ReDoc in the default/production app.
5. Add offline synthetic ASGI tests for the required auth matrix, every
   sensitive route, configuration failure, error redaction, valid behavior,
   docs exposure, and direct-Python behavior.
6. Document the trust boundary, configuration, request examples, fail-closed
   behavior, and the independent tax-sale/autonomous-write gates.
7. Run focused tests, relevant API tests, the safe full suite, diff/scope checks,
   and create the permitted local commit only if verification is green.

## Safety constraints

No real environment file, secret, PII, database, migration, external service,
sibling repository source, deployment, background process, or production system
will be accessed or modified.

## Verification record

- Focused HTTP authentication suite: `80 passed`.
- Relevant auth/receipt/compliance/hunt/recipe/insight/comps subset:
  `138 passed, 20 skipped` (database-dependent tests skipped against a synthetic
  loopback-denied URL).
- Full offline suite: `497 passed, 33 skipped, 1 failed`.
- The sole failure is the pre-existing
  `tests/test_load_parcel_geom.py::test_registry_entries_carry_full_provenance`:
  the test requires an `arcgis:` source while the unchanged Berrien registry
  uses `mineral-scout:`. Both files match `origin/main`.
- Python compilation and diff whitespace checks passed. No migration,
  dependency manifest, Fly, Docker, tax-sale, or production configuration file
  changed.
- Per the task's explicit commit rule, no commit was created because the full
  suite is not green, even though the authentication implementation and its
  relevant regression suites are green.

## Operator waiver — pre-existing unrelated test failure

The full offline suite completed with:

- 497 passed
- 33 database-dependent skipped
- 1 failed

The sole failure is:

`tests/test_load_parcel_geom.py::test_registry_entries_carry_full_provenance`

This failure is pre-existing on `origin/main` and is unrelated to HTTP authentication hardening. The unchanged Berrien registry configuration uses the `mineral-scout:` provenance prefix while the existing test expects every source to begin with `arcgis:`.

Ruling: waive this unrelated baseline failure for the authentication-hardening branch. Do not modify the geometry registry or its test in this security change. Resolve it separately.
