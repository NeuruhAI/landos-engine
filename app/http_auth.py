"""Fail-closed service authentication for the LandOS HTTP boundary."""

import secrets
from typing import Callable, Optional

from fastapi import Header, HTTPException


AUTHENTICATION_ERROR = "Unauthorized"
WWW_AUTHENTICATE = "Bearer"


def validate_service_token(name: str, value: Optional[str]) -> str:
    """Validate one configured value as a usable, non-placeholder HTTP token."""
    visible_ascii = bool(value) and all(0x21 <= ord(character) <= 0x7E for character in value)
    if (
        not value
        or not visible_ascii
        or value.casefold().startswith("replace")
    ):
        raise RuntimeError(
            f"Environment variable {name} must contain a usable service token"
        )
    return value


def _unauthorized() -> None:
    """Return one stable response without disclosing configuration or tokens."""
    raise HTTPException(
        status_code=401,
        detail=AUTHENTICATION_ERROR,
        headers={"WWW-Authenticate": WWW_AUTHENTICATE},
    )


def _parse_bearer(value: str) -> str:
    """Parse one strict ``Authorization: Bearer <token>`` value."""
    scheme, separator, token = value.partition(" ")
    if (
        not separator
        or scheme.casefold() != "bearer"
        or not token
        or any(character.isspace() for character in token)
    ):
        _unauthorized()
    return token


def _matches(presented: str, expected_tokens: tuple[str, ...]) -> bool:
    """Compare credentials in constant time, including non-ASCII attack input."""
    presented_bytes = presented.encode("utf-8")
    matched = False
    for expected in expected_tokens:
        matched |= secrets.compare_digest(
            presented_bytes,
            expected.encode("utf-8"),
        )
    return matched


def service_auth_dependency(
    service_token: str,
    *bearer_aliases: Optional[str],
) -> Callable[..., bool]:
    """Build a FastAPI dependency for one service-token scope.

    ``LANDOS_SECRET`` is the canonical credential. Optional route-scoped bearer
    aliases preserve existing integrations, but their absence never opens a
    route. The legacy ``X-LANDOS-SECRET`` transport accepts only the canonical
    credential and remains for backward compatibility.
    """
    service_token = validate_service_token("LANDOS_SECRET", service_token)

    accepted_bearer_tokens = (service_token,) + tuple(
        validate_service_token("route-scoped service token", token)
        for token in bearer_aliases
        if token is not None
    )

    def authenticate(
        authorization: Optional[str] = Header(default=None),
        x_landos_secret: Optional[str] = Header(default=None),
    ) -> bool:
        if authorization is not None:
            presented = _parse_bearer(authorization)
            expected_tokens = accepted_bearer_tokens
        elif x_landos_secret is not None:
            if not x_landos_secret or any(
                character.isspace() for character in x_landos_secret
            ):
                _unauthorized()
            presented = x_landos_secret
            expected_tokens = (service_token,)
        else:
            _unauthorized()

        if not _matches(presented, expected_tokens):
            _unauthorized()
        return True

    return authenticate
