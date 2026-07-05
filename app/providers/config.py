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
    # A2 cohort counties (2026-07-01): county_rest ONLY. local_cache would
    # self-hit the cohort's own owner_leads rows (owner_name NULL) and
    # short-circuit the network tier; reportall is quota-dead (alltime 1,000);
    # regrid is PAID and operator-gated (no key in .env). $0-tiers-only run.
    "26021": ["county_rest"],
    "26005": ["county_rest"],
    "26027": ["county_rest"],
    "26075": ["county_rest"],
    "26121": ["county_rest"],
    # ENRICH-1 statewide-MI cohort counties (2026-07-05): same $0-tier-only rationale.
    "26009": ["county_rest"],
    "26051": ["county_rest"],
    "26077": ["county_rest"],
    "26157": ["county_rest"],
}

# ---- tier 1: county REST endpoints --------------------------------------------
# Counties without a row make the provider raise ProviderUnavailable (the chain
# degrades past it cleanly).
#
# Shape per county (only url/apn_field/owner_field/verified are REQUIRED; the
# provider maps every *_field key it finds and leaves the rest honest-NULL):
#   url                 ArcGIS FeatureServer/MapServer layer query URL (…/query)
#   apn_field           attribute holding the county APN as the LAYER stores it
#   apn_prefix          literal prefix the layer prepends to our canonical APN
#                       (queries add it; returned parcel_apn strips it back)
#   owner_field         primary owner name
#   owner2_field        second owner name line (kept in raw_meta, not primary)
#   mail_street_field / mail_city_field / mail_state_field / mail_zip_field
#   situs_street_field / situs_city_field / situs_state_field / situs_zip_field
#   verified            False until an operator has confirmed endpoint + fields
#                       against a known parcel — the provider REFUSES to serve
#                       unverified endpoints (no fabricated matches from a
#                       guessed field mapping).
COUNTY_REST_ENDPOINTS = {
    # Berrien County, MI — county-hosted ArcGIS Enterprise (the old
    # gis.berriencounty.org stub was a dead host; real domain is
    # gis.berriencountymi.gov). VERIFIED 2026-07-01: layer pin carries the
    # county code prefix '11-'; exact-match pin='11-03-0015-0007-02-9' ->
    # BEILMAN BONITA, 485 WELLS AVE, BENTON HARBOR MI 49022 (situs matches the
    # tax-sale.info lot's Benton Harbor description). WAF NOTE: leading-'%'
    # LIKE queries return an HTML 'Blocked' page — use exact matches only.
    # Layer also carries sev24/taxable24/taxdesc for cross-checks.
    "26021": {
        "url": "https://gis.berriencountymi.gov/server/rest/services/Hosted/Parcel_2025/FeatureServer/0/query",
        "apn_field": "pin",
        "apn_prefix": "11-",
        "owner_field": "ownername1",
        "owner2_field": "ownername2",
        "mail_street_field": "owneradd",
        "mail_city_field": "ownercity",
        "mail_state_field": "ownerstate",
        "mail_zip_field": "ownerzip",
        "situs_street_field": "propadd",
        "situs_city_field": "propcity",
        "situs_state_field": "propstate",
        "situs_zip_field": "propzip",
        "verified": True,
        "note": "Parcel_2025 hosted layer; pin = '11-'+canonical APN; exact WHERE only (WAF).",
    },
    # Allegan County, MI — county-hosted ArcGIS ('Property Owner Label' layer 2
    # is a POLYGON layer carrying the full owner/mailing attribute set).
    # VERIFIED 2026-07-01: MAPPING_ID='01-021-010-00' (canonical form, no
    # prefix) -> TIMPH DONALD H, 3012 ROSE DRIVE, ALLEGAN MI 49010. Situs is
    # componentized ambiguously (propaddrnu/propStreet/propstre_*) — left
    # unmapped (honest NULL) rather than composed from guessed semantics.
    "26005": {
        "url": "https://gis.allegancounty.org/server/rest/services/Parcel_Viewer_Map_v1_MIL1/MapServer/2/query",
        "apn_field": "MAPPING_ID",
        "owner_field": "ownername1",
        "owner2_field": "ownername2",
        "mail_street_field": "ownerstree",
        "mail_city_field": "ownercity",
        "mail_state_field": "ownerstate",
        "mail_zip_field": "ownerzip",
        "verified": True,
        "note": "layer 2 'Property Owner Label'; MAPPING_ID = canonical APN verbatim.",
    },
    # Cass County, MI — county AGOL org (casscountymi.maps.arcgis.com,
    # orgid 2Pxs2zBdOuEfbewK). NB: 'Cass.maps.arcgis.com' is Cass County
    # NEBRASKA — do not use. VERIFIED 2026-07-01: Parcel_Num =
    # '14-010-016-277-00' (canonical form incl. the 14- county code, matching
    # our cohort APNs verbatim); Owner1/OwnerAdd/OwnerCity/OwnerState/OwnerZip
    # + Physical situs + SEV/Legal for cross-checks.
    "26027": {
        "url": "https://services3.arcgis.com/2Pxs2zBdOuEfbewK/arcgis/rest/services/county_parcels/FeatureServer/0/query",
        "apn_field": "Parcel_Num",
        "owner_field": "Owner1",
        "owner2_field": "Owner2",
        "mail_street_field": "OwnerAdd",
        "mail_city_field": "OwnerCity",
        "mail_state_field": "OwnerState",
        "mail_zip_field": "OwnerZip",
        "situs_street_field": "Physical",
        "situs_city_field": "City",
        "situs_state_field": "State",
        "situs_zip_field": "Zip",
        "verified": True,
        "note": "county_parcels hosted layer; Parcel_Num = canonical APN verbatim; SEV/Legal available for audit.",
    },
    # Jackson County, MI — county ArcGIS Enterprise. VERIFIED 2026-07-03 (3/3
    # samples, PRE-VESTING roll: HARBY ANTHONY A & JEAN V / JOHNSON TRAVIS /
    # P NINE REAL ESTATE LLC — real individuals+entities, not the treasurer).
    # PIN = canonical APN verbatim (incl. leading '000-').
    "26075": {
        "url": "https://gis.mijackson.org/countygis/rest/services/RealEstate/RealEstateParcels/FeatureServer/0/query",
        "apn_field": "PIN",
        "owner_field": "OWNER",
        "owner2_field": "OWNER2",
        "mail_street_field": "O_ADDRESS",
        "mail_city_field": "O_CITY",
        "mail_state_field": "O_STATE",
        "mail_zip_field": "O_ZIP",
        "situs_street_field": "P_FULLADD",
        "situs_city_field": "P_CITY",
        "situs_state_field": "P_STATE",
        "situs_zip_field": "P_ZIP",
        "verified": True,
        "note": "RealEstateParcels L0; PIN verbatim; situs P_FULLADD can be 'RD V/L' style (vacant).",
    },
    # Muskegon County, MI — county ArcGIS 10.91, PropertyViewer/MapServer
    # layer 25 'Parcels - LS' (layer 20 is a group wrapper; 23/24/25 are the
    # real scale variants). VERIFIED 2026-07-03 (3/3 samples): MIXED/partially
    # VESTED roll — 1/3 'COUNTY OF MUSKEGON TREASURER', 2/3 individuals (both
    # out-of-state absentees). classify_owner_type gates treasurer rows to
    # govt; NOT a uniform treasurer-roll stop. PIN carries '61-' county code;
    # PIN_No_Cnty = canonical APN verbatim (use it — no prefix round-trip).
    "26121": {
        "url": "https://maps.muskegoncountygis.com/arcgis/rest/services/PropertyViewer/MapServer/25/query",
        "apn_field": "PIN_No_Cnty",
        "owner_field": "Owner_Name1",
        "owner2_field": "Owner_Name2",
        "mail_street_field": "Owner_Address",
        "mail_city_field": "Owner_Address_City",
        "mail_state_field": "Owner_Address_State",
        "mail_zip_field": "Owner_Address_ZipCode",
        "situs_street_field": "Property_Address_Combined",
        "situs_city_field": "Property_Address_City",
        "situs_state_field": "Property_Address_State",
        "situs_zip_field": "Property_Address_ZipCode",
        "verified": True,
        "note": "MapServer 10.91 (pagination degrade applies); SEV field State_Equalized_Value for audit.",
    },
    # Antrim County, MI — AGOL AllParcels_shp. TREASURER ROLL (3/3 samples
    # 'ANTRIM COUNTY TREASURER', re-confirmed 2026-07-05): the 2026-07-03
    # paid-gate-stop ruling is SUPERSEDED by the ENRICH-1 vintage-capture
    # doctrine — the endpoint now serves so those rows get vintage_needed=true
    # (never a treasurer name in owner_name) + coords/situs the roll holds
    # honestly. O_ADDRESS often still carries a PRE-vesting mailing address —
    # stowed in raw_meta->'vintage'->'roll_mailing' for the paid capture.
    "26009": {
        "url": "https://services6.arcgis.com/ZcCP0G3kEnUDVego/arcgis/rest/services/AllParcels_shp/FeatureServer/0/query",
        "apn_field": "PARCEL_NO",
        "owner_field": "OWNER_NAME",
        "owner2_field": "OWNER_NAM2",
        "mail_street_field": "O_ADDRESS",
        "mail_city_field": "O_CITY",
        "mail_state_field": "O_STATE",
        "mail_zip_field": "O_ZIP",
        "verified": True,
        "note": "VERIFIED 2026-07-05 3/3 APNs; VESTED/treasurer roll -> vintage lane. PARCEL_NO = canonical APN verbatim.",
    },
    # Gladwin County, MI — AGOL Gladwin_County_Parcels. VERIFIED 2026-07-05
    # 3/3 samples, PRE-VESTING roll (GREENE RONALD / LAND OF LAND INC /
    # MARSHALL DEBRA — real owners, not the FGU). OwnerAdd_1 is a combined
    # 'CITY ST ZIP' line — NOT componentized; city/state/zip left honest-NULL
    # rather than parsed from guessed semantics (full line rides in raw_meta).
    "26051": {
        "url": "https://services.arcgis.com/edGXqVKidagzAIPB/arcgis/rest/services/Gladwin_County_Parcels/FeatureServer/0/query",
        "apn_field": "ParcelNumb",
        "owner_field": "OwnerName",
        "mail_street_field": "OwnerAddre",
        "verified": True,
        "note": "VERIFIED 2026-07-05 3/3; live roll; ParcelNumb = canonical APN verbatim; OwnerAdd_1 (city/st/zip combined) unmapped.",
    },
    # Kalamazoo County, MI — county-hosted ArcGIS Enterprise, Parcels (2025)
    # view. VERIFIED 2026-07-05 2/3 samples (1 APN absent from layer), LIVE
    # roll (HUDSON CARL J & PAULINE O / PEREZ MICHAEL F & AMANDA A). Field
    # names are munged but consistent: onername1/onerstate/otaddress/orzipcode
    # (owner) vs pcombined/presscity/pessstate/pdresszip (situs) — mapping
    # confirmed against real response values (601 vs 602 COLLETTE AVE).
    "26077": {
        "url": "https://gis.kalcounty.com/server/rest/services/Hosted/Parcels__2025__view/FeatureServer/0/query",
        "apn_field": "pelnumber",
        "owner_field": "onername1",
        "mail_street_field": "otaddress",
        "mail_city_field": "ownercity",
        "mail_state_field": "onerstate",
        "mail_zip_field": "orzipcode",
        "situs_street_field": "pcombined",
        "situs_city_field": "presscity",
        "situs_state_field": "pessstate",
        "situs_zip_field": "pdresszip",
        "verified": True,
        "note": "VERIFIED 2026-07-05 2/3; live roll; pelnumber = canonical APN verbatim (munged field names are the layer's own).",
    },
    # Tuscola County, MI — AGOL TuscolaCountyParcelsCurrentYearAddresses L53.
    # VERIFIED 2026-07-05 3/3 samples but TREASURER ROLL ('TUSCOLA COUNTY
    # TREASURER' 3/3) -> vintage lane, same doctrine as Antrim.
    "26157": {
        "url": "https://services6.arcgis.com/FA2StDZ4LO95Cm3q/arcgis/rest/services/TuscolaCountyParcelsCurrentYearAddresses/FeatureServer/53/query",
        "apn_field": "PIN",
        "owner_field": "ownername1",
        "owner2_field": "ownername2",
        "mail_street_field": "ownerstreetaddr",
        "mail_city_field": "ownercity",
        "mail_state_field": "ownerstate",
        "mail_zip_field": "ownerzip",
        "verified": True,
        "note": "VERIFIED 2026-07-05 3/3; VESTED/treasurer roll -> vintage lane. PIN = canonical APN verbatim.",
    },
    # Van Buren County, MI (26159): NO FREE SOURCE (STEP-2d stop, 2026-07-01).
    # County portal is MangoMap (API tier returns 402/payment-disabled), BS&A
    # owner search sits behind a login wall, no county/AGOL REST found. The 29
    # cohort rows stay unenriched until an operator approves a paid tier or a
    # new free source appears.
    # St. Joseph County, IN (18141): county GIS is Beacon/Schneider-hosted —
    # no open ArcGIS REST endpoint known; tier 1 stays unavailable there and
    # the chain degrades straight to tier 2.
}
