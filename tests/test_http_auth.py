"""Offline regression tests for the LandOS Engine HTTP trust boundary."""

import asyncio
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import types
from urllib.parse import urlsplit

import pytest


ROOT = Path(__file__).resolve().parents[1]
SERVICE_TOKEN = "test-only-landos-token-0000000000000000"
NVE_TOKEN = "test-only-nve-token-000000000000000000"
COMPOSER_TOKEN = "test-only-composer-token-0000000000"
INVALID_TOKEN = "test-only-invalid-token-000000000000000"
DATABASE_URL = "postgresql://synthetic:synthetic@127.0.0.1:1/landos"


class ForbiddenPool:
    """Sentinel that proves auth/liveness tests never touch a database."""

    def __init__(self):
        self.connection_calls = 0

    def connection(self):
        self.connection_calls += 1
        raise AssertionError("HTTP boundary test reached the database pool")


class PassiveDatabaseContext:
    """Allows context entry while rejecting every real database operation."""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def cursor(self):
        return self

    def execute(self, *_args, **_kwargs):
        raise AssertionError("Valid-auth route test attempted a database query")


class PassivePool:
    def __init__(self):
        self.connection_calls = 0

    def connection(self):
        self.connection_calls += 1
        return PassiveDatabaseContext()


@pytest.fixture
def app_factory(monkeypatch):
    def build(*, nve_token=None, composer_token=None):
        monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        monkeypatch.setenv("LANDOS_SECRET", SERVICE_TOKEN)
        if nve_token is None:
            monkeypatch.delenv("NVE_LAND_TOKEN", raising=False)
        else:
            monkeypatch.setenv("NVE_LAND_TOKEN", nve_token)
        if composer_token is None:
            monkeypatch.delenv("COMPOSER_TOKEN", raising=False)
        else:
            monkeypatch.setenv("COMPOSER_TOKEN", composer_token)

        for module_name in (
            "app.main",
            "app.config",
            "app.http_auth",
            "app.http_config",
        ):
            monkeypatch.delitem(sys.modules, module_name, raising=False)

        pool = ForbiddenPool()
        fake_db = types.ModuleType("app.db")
        fake_db.pool = pool
        monkeypatch.setitem(sys.modules, "app.db", fake_db)

        module = importlib.import_module("app.main")
        return module.app, pool

    return build


async def _asgi_request(app, method, target, *, headers=None, json_body=None):
    parsed = urlsplit(target)
    raw_body = b"" if json_body is None else json.dumps(json_body).encode("utf-8")
    raw_headers = [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in (headers or {}).items()
    ]
    if json_body is not None:
        raw_headers.extend(
            [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(raw_body)).encode("ascii")),
            ]
        )

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": parsed.path,
        "raw_path": parsed.path.encode("ascii"),
        "query_string": parsed.query.encode("ascii"),
        "root_path": "",
        "headers": raw_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "state": {},
    }
    request_messages = [
        {"type": "http.request", "body": raw_body, "more_body": False}
    ]
    response_messages = []

    async def receive():
        if request_messages:
            return request_messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(message):
        response_messages.append(message)

    await app(scope, receive, send)
    start = next(
        message
        for message in response_messages
        if message["type"] == "http.response.start"
    )
    body = b"".join(
        message.get("body", b"")
        for message in response_messages
        if message["type"] == "http.response.body"
    )
    response_headers = {
        name.decode("latin-1").lower(): value.decode("latin-1")
        for name, value in start["headers"]
    }
    return start["status"], response_headers, body


def request(app, method, target, *, headers=None, json_body=None):
    return asyncio.run(
        _asgi_request(
            app,
            method,
            target,
            headers=headers,
            json_body=json_body,
        )
    )


def bearer(token=SERVICE_TOKEN):
    return {"authorization": f"Bearer {token}"}


def assert_unauthorized(response):
    status, headers, body = response
    assert status == 401
    assert headers["www-authenticate"] == "Bearer"
    assert json.loads(body) == {"detail": "Unauthorized"}
    decoded = body.decode("utf-8")
    for token in (SERVICE_TOKEN, NVE_TOKEN, COMPOSER_TOKEN, INVALID_TOKEN):
        assert token not in decoded


def test_public_health_is_minimal_and_dependency_free(app_factory):
    app, pool = app_factory()
    status, _, body = request(app, "GET", "/health")
    assert status == 200
    assert json.loads(body) == {"status": "ok"}
    assert pool.connection_calls == 0


@pytest.mark.parametrize(
    ("header_name", "header_value"),
    [
        (None, None),
        ("authorization", ""),
        ("authorization", "Bearer"),
        ("authorization", "Bearer "),
        ("authorization", "Bearer token with spaces"),
        ("authorization", "Basic dGVzdDp0ZXN0"),
        ("authorization", f"Bearer {INVALID_TOKEN}"),
        ("x-landos-secret", ""),
    ],
    ids=[
        "missing",
        "empty",
        "malformed-no-token",
        "malformed-empty-bearer",
        "malformed-spaces",
        "unsupported-scheme",
        "invalid-bearer",
        "empty-legacy-header",
    ],
)
def test_invalid_authentication_fails_closed(app_factory, header_name, header_value):
    app, pool = app_factory()
    headers = {} if header_name is None else {header_name: header_value}
    response = request(app, "POST", "/land-dd", headers=headers, json_body={})
    assert_unauthorized(response)
    assert pool.connection_calls == 0


@pytest.mark.parametrize(
    "headers",
    [
        {"authorization": f"Bearer {SERVICE_TOKEN}"},
        {"authorization": f"bEaReR {SERVICE_TOKEN}"},
        {"x-landos-secret": SERVICE_TOKEN},
    ],
    ids=["bearer", "case-insensitive-bearer", "legacy-header"],
)
def test_valid_service_authentication_preserves_land_dd_behavior(app_factory, headers):
    app, pool = app_factory()
    status, _, body = request(app, "POST", "/land-dd", headers=headers, json_body={})
    assert status == 200
    payload = json.loads(body)
    assert payload["stance"] == "INSUFFICIENT_DATA"
    assert payload["deal_killers"] == ["county_fips required"]
    assert pool.connection_calls == 0


def test_missing_optional_alias_does_not_open_or_disable_route(app_factory):
    app, _ = app_factory()
    assert_unauthorized(request(app, "POST", "/land-dd", json_body={}))
    status, _, _ = request(
        app, "POST", "/land-dd", headers=bearer(), json_body={}
    )
    assert status == 200


def test_nve_alias_is_valid_only_for_its_route_scope(app_factory):
    app, pool = app_factory(nve_token=NVE_TOKEN)
    status, _, _ = request(
        app, "POST", "/land-dd", headers=bearer(NVE_TOKEN), json_body={}
    )
    assert status == 200
    assert_unauthorized(
        request(
            app,
            "POST",
            "/evaluate",
            headers=bearer(NVE_TOKEN),
            json_body={
                "tenant_id": "synthetic-tenant",
                "parcel_apn": "synthetic-apn",
                "owner_hash": "synthetic-owner-hash",
            },
        )
    )
    assert pool.connection_calls == 0


def test_composer_alias_is_valid_only_for_its_route_scope(app_factory):
    app, pool = app_factory(composer_token=COMPOSER_TOKEN)
    status, _, _ = request(
        app, "POST", "/compose", headers=bearer(COMPOSER_TOKEN), json_body={}
    )
    assert status == 422  # Authentication passed; request validation stayed separate.
    assert_unauthorized(
        request(
            app,
            "POST",
            "/land-dd",
            headers=bearer(COMPOSER_TOKEN),
            json_body={},
        )
    )
    assert pool.connection_calls == 0


SENSITIVE_ROUTES = [
    ("POST", "/land-dd", {}),
    ("GET", "/lead-receipt/synthetic-lead", None),
    ("GET", "/v1/insight/synthetic-lead", None),
    ("GET", "/v1/valuation/synthetic-lead", None),
    ("GET", "/compliance-cert/synthetic-lead", None),
    ("POST", "/compliance-cert/batch", {"lead_ids": ["synthetic-lead"]}),
    ("POST", "/hunt", {"territory": [{"fips": "00000"}]}),
    ("GET", "/hunt/synthetic-job", None),
    ("POST", "/compose", {"goal_type": "acquire", "goal_text": "synthetic"}),
    ("POST", "/proposals/synthetic-proposal/decide", {"action": "reject"}),
    (
        "POST",
        "/evaluate",
        {
            "tenant_id": "synthetic-tenant",
            "parcel_apn": "synthetic-apn",
            "owner_hash": "synthetic-owner-hash",
        },
    ),
    (
        "POST",
        "/claim",
        {
            "tenant_id": "synthetic-tenant",
            "node_id": "synthetic-node",
            "claimed_by": "synthetic-actor",
        },
    ),
    (
        "POST",
        "/release",
        {"tenant_id": "synthetic-tenant", "node_id": "synthetic-node"},
    ),
    (
        "POST",
        "/feedback",
        {
            "tenant_id": "synthetic-tenant",
            "node_id": "synthetic-node",
            "outcome": "passed",
        },
    ),
    ("GET", "/leads/top?tenant_id=synthetic-tenant", None),
    ("GET", "/emerging/heat-map?tenant_id=synthetic-tenant", None),
]

EXPECTED_CAPABILITY_ROUTES = {
    ("POST", "/land-dd"),
    ("GET", "/lead-receipt/{lead_id}"),
    ("GET", "/v1/insight/{lead_id}"),
    ("GET", "/v1/valuation/{lead_id}"),
    ("GET", "/compliance-cert/{lead_id}"),
    ("POST", "/compliance-cert/batch"),
    ("POST", "/hunt"),
    ("GET", "/hunt/{job_id}"),
    ("POST", "/compose"),
    ("POST", "/proposals/{proposal_id}/decide"),
    ("POST", "/evaluate"),
    ("POST", "/claim"),
    ("POST", "/release"),
    ("POST", "/feedback"),
    ("GET", "/leads/top"),
    ("GET", "/emerging/heat-map"),
}


def test_route_inventory_has_one_public_path_and_no_untracked_surface(app_factory):
    app, _ = app_factory()
    actual_routes = {
        (method, route.path)
        for route in app.routes
        for method in route.methods
    }
    assert actual_routes == EXPECTED_CAPABILITY_ROUTES | {("GET", "/health")}


@pytest.mark.parametrize(
    ("method", "target", "body"),
    SENSITIVE_ROUTES,
    ids=[target for _, target, _ in SENSITIVE_ROUTES],
)
def test_every_capability_route_rejects_missing_auth(
    app_factory, method, target, body
):
    app, pool = app_factory()
    assert_unauthorized(request(app, method, target, json_body=body))
    assert pool.connection_calls == 0


@pytest.mark.parametrize(
    ("method", "target", "body"),
    SENSITIVE_ROUTES,
    ids=[target for _, target, _ in SENSITIVE_ROUTES],
)
def test_every_capability_route_rejects_wrong_auth(
    app_factory, method, target, body
):
    app, pool = app_factory()
    assert_unauthorized(
        request(
            app,
            method,
            target,
            headers=bearer(INVALID_TOKEN),
            json_body=body,
        )
    )
    assert pool.connection_calls == 0


VALID_AUTH_ROUTES = [
    ("POST", "/land-dd", {}, 200),
    ("GET", "/lead-receipt/synthetic-lead", None, 404),
    ("GET", "/v1/insight/synthetic-lead?force=not-a-bool", None, 422),
    ("GET", "/v1/valuation/synthetic-lead?federal=not-a-bool", None, 422),
    ("GET", "/compliance-cert/synthetic-lead", None, 404),
    ("POST", "/compliance-cert/batch", {"lead_ids": []}, 422),
    ("POST", "/hunt", {"territory": []}, 422),
    ("GET", "/hunt/synthetic-job", None, 404),
    ("POST", "/compose", {}, 422),
    ("POST", "/proposals/synthetic-proposal/decide", {}, 422),
    ("POST", "/evaluate", {}, 422),
    ("POST", "/claim", {}, 422),
    ("POST", "/release", {}, 422),
    ("POST", "/feedback", {}, 422),
    ("GET", "/leads/top?tenant_id=synthetic&limit=not-an-int", None, 422),
    (
        "GET",
        "/emerging/heat-map?tenant_id=synthetic&limit=not-an-int",
        None,
        422,
    ),
]


@pytest.mark.parametrize(
    ("method", "target", "body", "expected_status"),
    VALID_AUTH_ROUTES,
    ids=[target for _, target, _, _ in VALID_AUTH_ROUTES],
)
def test_every_capability_route_accepts_correct_auth_without_external_io(
    app_factory,
    monkeypatch,
    method,
    target,
    body,
    expected_status,
):
    app, forbidden_pool = app_factory()
    main_module = sys.modules["app.main"]
    passive_pool = PassivePool()
    monkeypatch.setattr(main_module, "pool", passive_pool)
    monkeypatch.setattr(
        main_module.receipt_mod,
        "assemble_receipt",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        main_module.hunt_core,
        "get_job",
        lambda *_args, **_kwargs: None,
    )

    status, _, response_body = request(
        app,
        method,
        target,
        headers=bearer(),
        json_body=body,
    )
    assert status == expected_status
    assert status != 401
    assert SERVICE_TOKEN not in response_body.decode("utf-8")
    assert forbidden_pool.connection_calls == 0


@pytest.mark.parametrize(
    "target", ["/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"]
)
def test_documentation_and_schema_are_disabled_by_default(app_factory, target):
    app, pool = app_factory()
    status, _, _ = request(app, "GET", target, headers=bearer())
    assert status == 404
    assert pool.connection_calls == 0


def test_authentication_failure_is_distinct_from_validation_failure(app_factory):
    app, pool = app_factory()
    assert_unauthorized(
        request(
            app,
            "POST",
            "/evaluate",
            headers=bearer(INVALID_TOKEN),
            json_body={},
        )
    )
    status, _, body = request(
        app, "POST", "/evaluate", headers=bearer(), json_body={}
    )
    assert status == 422
    assert json.loads(body)["detail"]
    assert pool.connection_calls == 0


@pytest.mark.parametrize(
    "configured_value",
    [None, "", "   ", "replace-with-a-real-secret"],
    ids=["missing", "empty", "whitespace", "placeholder"],
)
def test_unusable_server_authentication_configuration_stops_construction(
    configured_value,
):
    env = {
        "DATABASE_URL": DATABASE_URL,
        "PYTHONPATH": str(ROOT),
        "PYTHON_DOTENV_DISABLED": "1",
    }
    if configured_value is not None:
        env["LANDOS_SECRET"] = configured_value
    if "PATH" in os.environ:
        env["PATH"] = os.environ["PATH"]
    result = subprocess.run(
        [sys.executable, "-c", "import app.http_config"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "LANDOS_SECRET" in output
    assert SERVICE_TOKEN not in output
    assert INVALID_TOKEN not in output


@pytest.mark.parametrize(
    "alias_value",
    ["", "   ", "replace-with-a-real-secret", "non-ascii-\N{LATIN SMALL LETTER E WITH ACUTE}"],
    ids=["empty", "whitespace", "placeholder", "non-ascii"],
)
def test_unusable_optional_alias_stops_http_construction(alias_value):
    env = {
        "DATABASE_URL": DATABASE_URL,
        "LANDOS_SECRET": SERVICE_TOKEN,
        "NVE_LAND_TOKEN": alias_value,
        "PYTHONPATH": str(ROOT),
        "PYTHON_DOTENV_DISABLED": "1",
    }
    if "PATH" in os.environ:
        env["PATH"] = os.environ["PATH"]
    result = subprocess.run(
        [sys.executable, "-c", "import app.http_config"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode != 0
    assert "NVE_LAND_TOKEN" in result.stderr
    assert SERVICE_TOKEN not in result.stderr


def test_malformed_http_alias_does_not_break_shared_cli_configuration():
    env = {
        "DATABASE_URL": DATABASE_URL,
        "LANDOS_SECRET": SERVICE_TOKEN,
        "NVE_LAND_TOKEN": "   ",
        "COMPOSER_TOKEN": "replace-with-a-real-secret",
        "PYTHONPATH": str(ROOT),
        "PYTHON_DOTENV_DISABLED": "1",
    }
    if "PATH" in os.environ:
        env["PATH"] = os.environ["PATH"]
    result = subprocess.run(
        [sys.executable, "-c", "import app.config"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0


def test_all_configured_tokens_are_compared_without_short_circuit(monkeypatch):
    from app import http_auth

    original = http_auth.secrets.compare_digest
    comparisons = []

    def record_comparison(presented, expected):
        comparisons.append((presented, expected))
        return original(presented, expected)

    monkeypatch.setattr(http_auth.secrets, "compare_digest", record_comparison)
    assert http_auth._matches(SERVICE_TOKEN, (SERVICE_TOKEN, NVE_TOKEN))
    assert len(comparisons) == 2


def test_direct_internal_scoring_does_not_require_http_authentication():
    from app.scoring import DEFAULT_WEIGHTS, score_parcel

    result = score_parcel(
        {
            "distress": {"tax_delinquent": True},
            "owner": {"out_of_state": True},
            "arbitrage": {},
        },
        DEFAULT_WEIGHTS,
    )
    assert result["score"] > 0
    assert result["top_factors"]
