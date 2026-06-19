"""
config.py — provider chain order + per-county endpoints. OPERATOR-EDITABLE.

The chain order is DATA, not logic: reorder tiers here (globally or per
county / white-label tenant) without touching chain.py. Cost gradient,
cheapest first. Adding a vendor later (e.g. Regrid) = its config row + a key
in .env — no code change to the chain.
"""

# ---- chain order -------------------------------------------------------------
# key = county_fips for a per-county override, 'default' otherwise.
PARCEL_PROVIDER_CHAIN = {
    "default": ["local_cache", "county_rest", "reportall", "regrid"],
    # example per-county override (uncomment to prefer county REST over local):
    # "26021": ["county_rest", "local_cache", "reportall", "regrid"],
}

# ---- tier 1: county REST endpoints --------------------------------------------
# One worked stub documented below; counties without a row make the provider
# raise ProviderUnavailable (the chain degrades past it cleanly).
#
# Shape per county:
#   url           ArcGIS FeatureServer/MapServer layer query URL (…/query)
#   apn_field     attribute holding the CANONICAL county APN
#   owner_field   attribute holding the owner name
#   verified      False until an operator has confirmed the endpoint + fields
#                 against a known parcel — the provider REFUSES to serve
#                 unverified endpoints (no fabricated matches from a guessed
#                 field mapping).
COUNTY_REST_ENDPOINTS = {
    # WORKED STUB — Berrien County, MI (26021). Berrien publishes parcels via
    # an ArcGIS instance (gis.berriencounty.org). Endpoint + field names must
    # be confirmed by an operator before flipping verified=True; the layer
    # path below is the documented starting point, NOT a verified fact.
    "26021": {
        "url": "https://gis.berriencounty.org/arcgis/rest/services/Parcels/MapServer/0/query",
        "apn_field": "PIN",
        "owner_field": "OWNER_NAME",
        "verified": False,   # flip after operator confirms one known parcel round-trips
        "note": "TODO-verify: confirm service path + field names against a known APN "
                "(e.g. 11-54-0340-0145-00-9) before enabling.",
    },
    # St. Joseph County, IN (18141): county GIS is Beacon/Schneider-hosted —
    # no open ArcGIS REST endpoint known; tier 1 stays unavailable there and
    # the chain degrades straight to tier 2.
}
