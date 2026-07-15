"""HTTP-only authentication configuration for the LandOS API process."""

import os
from typing import Optional

from .config import LANDOS_SECRET as CONFIGURED_LANDOS_SECRET
from .http_auth import validate_service_token


def _optional_service_token(name: str) -> Optional[str]:
    if name not in os.environ:
        return None
    return validate_service_token(name, os.environ.get(name))


LANDOS_SECRET = validate_service_token(
    "LANDOS_SECRET",
    CONFIGURED_LANDOS_SECRET,
)
NVE_LAND_TOKEN = _optional_service_token("NVE_LAND_TOKEN")
COMPOSER_TOKEN = _optional_service_token("COMPOSER_TOKEN")
