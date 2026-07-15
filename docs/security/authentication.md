# LandOS HTTP Authentication

## Trust boundary

The LandOS Engine HTTP service is a machine-to-machine boundary that may be
reachable over public HTTPS. `GET /health` is the sole public endpoint. It
returns only `{"status":"ok"}` and does not inspect or disclose databases,
providers, configuration, tenants, counts, paths, or implementation details.

Every engine-capability route requires service authentication. OpenAPI,
Swagger UI, and ReDoc are disabled in the default application.

## Configuration

- `LANDOS_SECRET` is required at application construction and is the canonical
  service token. Missing, blank, whitespace-padded, or placeholder values stop
  the application from booting.
- `NVE_LAND_TOKEN` is an optional scoped Bearer alias for the land-dd,
  receipt, insight, valuation, compliance-certificate, and hunt routes.
- `COMPOSER_TOKEN` is an optional scoped Bearer alias for compose and proposal
  decision routes.

Optional aliases never replace the required canonical token. If an alias is
absent, its routes still require `LANDOS_SECRET`. If an alias is explicitly
present but blank or placeholder-valued, application construction fails.

## Authenticated requests

Bearer authentication is canonical:

```bash
curl -sS https://<engine-host>/leads/top?tenant_id=<tenant-id> \
  -H 'Authorization: Bearer <LANDOS_SECRET>'
```

The existing compatibility header remains accepted only with the canonical
token:

```bash
curl -sS https://<engine-host>/leads/top?tenant_id=<tenant-id> \
  -H 'X-LANDOS-SECRET: <LANDOS_SECRET>'
```

Missing, empty, malformed, unsupported, or invalid credentials return a stable
`401` body, `{"detail":"Unauthorized"}`, with `WWW-Authenticate: Bearer`.
Responses and logs never include submitted or expected token material.

## Independent authorization gates

Service authentication establishes only that an approved caller possesses a
configured service credential. It does not establish tenant-level identity,
approve autonomous production writes, authorize outreach or spend, or replace
human governance receipts.

Tax-sale minting is subject to a separate human-promotion requirement that is
not yet enforced end to end and must remain disarmed. This authentication
change does not remove, bypass, or automatically satisfy that requirement.
Keep minting disarmed until that bounded follow-up is implemented and reviewed.
