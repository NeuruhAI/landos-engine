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
    # ArcGIS-served counties -> county_rest; FetchGIS PSsearch2 counties -> fetchgis;
    # Colligo public-link counties -> colligo. One tier each, evidence-verified.
    "26009": ["county_rest"], "26051": ["county_rest"], "26077": ["county_rest"],
    "26157": ["county_rest"], "26015": ["county_rest"], "26133": ["county_rest"],
    "26115": ["county_rest"], "26017": ["county_rest"], "26143": ["county_rest"],
    "26011": ["county_rest"], "26031": ["county_rest"], "26095": ["county_rest"],
    "26019": ["county_rest"], "26085": ["county_rest"], "26029": ["county_rest"],
    "26067": ["county_rest"], "26103": ["county_rest"], "26147": ["county_rest"],
    "26117": ["county_rest"],
    "26107": ["fetchgis"], "26025": ["fetchgis"], "26137": ["fetchgis"],
    "26149": ["fetchgis"], "26041": ["fetchgis"], "26033": ["fetchgis"],
    "26155": ["fetchgis"], "26151": ["fetchgis"], "26003": ["fetchgis"],
    "26097": ["fetchgis"], "26153": ["fetchgis"], "26035": ["fetchgis"],
    "26063": ["fetchgis"], "26129": ["fetchgis"], "26069": ["fetchgis"],
    "26127": ["fetchgis"], "26087": ["fetchgis"], "26057": ["fetchgis"],
    "26073": ["fetchgis"], "26037": ["fetchgis"],
    "26079": ["colligo"], "26061": ["colligo"], "26053": ["colligo"],
    "26039": ["colligo"],
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
    # ---- ENRICH-1 statewide-MI ArcGIS counties (census fan-out, 2026-07-05) ----
    # Every entry APN-verified against >=1 real cohort parcel returning a non-null
    # owner. 'VESTED' notes = treasurer/state roll counties (vintage lane, guard
    # in enrich_owner_chain). Combined-field mappings left NULL where the layer
    # doesn't componentize (never parsed from guessed semantics).
    "26015": {  # Barry — LIVE roll (LA COURSE ROBERT 3/3). PublicViewer NOT OpenData (owner-stripped).
        "url": "https://services.arcgis.com/i0SsdDzLI3mLGmPR/arcgis/rest/services/Parcels_PublicViewer_View/FeatureServer/14/query",
        "apn_field": "Tparcel", "owner_field": "owner_name",
        "mail_street_field": "owner_street_add", "mail_city_field": "owner_city",
        "mail_state_field": "owner_state", "mail_zip_field": "owner_zip",
        "situs_street_field": "prop_street_add", "situs_city_field": "property_city",
        "situs_zip_field": "property_zip", "verified": True,
        "note": "VERIFIED 2026-07-05 3/3; live roll; APN as-is dashed."},
    "26133": {  # Osceola — VESTED (OSCEOLA COUNTY TREASURER 3/3); layer itself current-roll.
        "url": "https://services8.arcgis.com/FmKMwUEmDSC75SQm/arcgis/rest/services/OsceolaCountyParcels_view/FeatureServer/0/query",
        "apn_field": "PIN", "owner_field": "OWNER",
        "situs_street_field": "PROPADD", "situs_city_field": "PROPCITY",
        "situs_state_field": "PROPSTATE", "situs_zip_field": "PROPZIP",
        "verified": True, "note": "VERIFIED 2026-07-05 3/3; APN space-separated as-is; vintage lane."},
    "26115": {  # Monroe — LIVE roll (BONILLA JOSE 3/3); BS&A ParcelMaster join, full mailing.
        "url": "https://gis.mocogeo.org/server/rest/services/Hosted/Parcel_View_2026_view/FeatureServer/0/query",
        "apn_field": "parcelid", "owner_field": "parcelmaster_ownername1",
        "owner2_field": "parcelmaster_ownername2",
        "mail_street_field": "parcelmaster_ownerstreetaddr",
        "mail_city_field": "parcelmaster_ownercity",
        "mail_state_field": "parcelmaster_ownerstate",
        "mail_zip_field": "parcelmaster_ownerzip",
        "situs_street_field": "parcelmaster_propstreetcombined",
        "situs_city_field": "parcelmaster_propcity",
        "situs_zip_field": "parcelmaster_propzip", "verified": True,
        "note": "VERIFIED 2026-07-05 3/3; APN space-separated as-is."},
    "26017": {  # Bay — LIVE roll (LEVEL 10 CONSULTING LLC 3/3, incl out-of-state mail).
        "url": "https://services7.arcgis.com/NsdAMAHBh4C0qHfJ/arcgis/rest/services/Parcels2025/FeatureServer/0/query",
        "apn_field": "PARCELID", "owner_field": "ownername1", "owner2_field": "ownername2",
        "mail_street_field": "ownerstree", "mail_city_field": "ownercity",
        "mail_state_field": "ownerstate", "mail_zip_field": "ownerzip",
        "situs_street_field": "propstreet", "situs_city_field": "propcity",
        "situs_state_field": "propstate", "situs_zip_field": "propzip",
        "verified": True, "note": "VERIFIED 2026-07-05 3/3 (org baycogis — NOT Bay County FL); APN dashed as-is."},
    "26143": {  # Roscommon — LIVE roll (VAN RIPER TIMOTHY J 3/3, incl NV absentee).
        "url": "https://services.arcgis.com/edGXqVKidagzAIPB/arcgis/rest/services/Roscommon_County_Parcels/FeatureServer/0/query",
        "apn_field": "PIN", "owner_field": "ownername1", "owner2_field": "ownername2",
        "mail_street_field": "ownerstree", "mail_city_field": "ownercity",
        "mail_state_field": "ownerstate", "mail_zip_field": "ownerzip",
        "situs_street_field": "Property_A", "verified": True,
        "note": "VERIFIED 2026-07-05 3/3; APN dashed 3-3-3-4 as-is."},
    "26011": {  # Arenac — LIVE roll (MYR RUTH ANN & TERRY 3/3). Sublayer 34, not 0.
        "url": "https://services8.arcgis.com/SWvtgOskziun2bFF/arcgis/rest/services/Arenac_Parcels_2026/FeatureServer/34/query",
        "apn_field": "PID", "owner_field": "OWNERNAME", "owner2_field": "OWNERNAME2",
        "mail_street_field": "OWNER_ADDR", "situs_street_field": "PROPERTY_A",
        "verified": True,
        "note": "VERIFIED 2026-07-05 3/3; OWNER_ADDR = combined 'street, city, state zip' (city/state/zip unmapped)."},
    "26031": {  # Cheboygan — VESTED (CHEBOYGAN COUNTY TREASURER 3/3); layer at large has humans.
        "url": "https://services6.arcgis.com/j6ueqNZduqQcCpQQ/arcgis/rest/services/Relationship_class/FeatureServer/0/query",
        "apn_field": "PARCEL_NO", "owner_field": "ownername1", "owner2_field": "ownername2",
        "mail_street_field": "ownerstreetaddr", "mail_city_field": "ownercity",
        "mail_state_field": "ownerstate", "mail_zip_field": "ownerzip",
        "situs_street_field": "propstreetcombined", "situs_city_field": "propcity",
        "situs_zip_field": "propzip", "verified": True,
        "note": "VERIFIED 2026-07-05 3/3; APN dashed as-is; vintage lane."},
    "26095": {  # Luce — MIXED roll (MONCK CHRISTINA L + STATE OF MICHIGAN; 2/3 matched).
        "url": "https://services.arcgis.com/r3fID69a2yHQdVc4/arcgis/rest/services/LuceCoCensusParcels/FeatureServer/0/query",
        "apn_field": "PID", "owner_field": "OWNER",
        "mail_street_field": "mailg_addr", "mail_city_field": "mailg_city",
        "mail_state_field": "mailg_stat", "mail_zip_field": "mailg_zip",
        "situs_street_field": "situs_addr", "situs_city_field": "situs_city",
        "situs_state_field": "situs_stat", "situs_zip_field": "situs_zip",
        "verified": True, "note": "VERIFIED 2026-07-05 2/3 (EUP regional layer); mailg_* often blank."},
    "26019": {  # Benzie — LIVE roll (BOLTON, MICHAEL 3/3). OWNERCSZ combined (unmapped).
        "url": "https://services5.arcgis.com/lcdRo4JSOTVu3FMR/arcgis/rest/services/Benzie_County_Parcels/FeatureServer/1/query",
        "apn_field": "PNUM", "owner_field": "OWNERNAME1",
        "mail_street_field": "OWNERSTREE", "situs_street_field": "PROPSTREET",
        "verified": True, "note": "VERIFIED 2026-07-05 3/3; APN dashed as-is."},
    "26085": {  # Lake — VESTED: bare owner value 'STATE' 3/3 (State of Michigan shorthand).
        "url": "https://services6.arcgis.com/077PZZsPIOhJKfvk/arcgis/rest/services/Parcels2026_View/FeatureServer/0/query",
        "apn_field": "PARCELID", "owner_field": "OWNERNAME",
        "mail_street_field": "OWNERADDRESS", "mail_city_field": "OWNERCITY",
        "mail_state_field": "OWNERSTATE", "mail_zip_field": "OWNERZIP",
        "situs_street_field": "PROPADDRESS", "verified": True,
        "note": "VERIFIED 2026-07-05 3/3; vintage lane (bare 'STATE' caught by guard's ^STATE$)."},
    "26029": {  # Charlevoix — VESTED (STATE OF MICHIGAN / MI DNR 3/3). THIRD-PARTY snapshot org.
        "url": "https://services8.arcgis.com/iOTGKDZaCCAubV8c/arcgis/rest/services/charlevoix_parcels_shp/FeatureServer/0/query",
        "apn_field": "pin", "owner_field": "owner", "owner2_field": "owner2",
        "mail_street_field": "address", "mail_city_field": "city",
        "mail_state_field": "state", "mail_zip_field": "zip",
        "situs_street_field": "propstreet", "situs_city_field": "propcity",
        "situs_state_field": "propstate", "situs_zip_field": "propzip",
        "verified": True,
        "note": "VERIFIED 2026-07-05 3/3 but NOT county-official hosting — snapshot-dated roll; county portals are closed (MangoMap/PivotPoint)."},
    "26067": {  # Ionia — MIXED roll (ALLEN MATTHEW + 'IONIA COUNTY TREASRUER' sic). Layer PIN carries '34-'.
        "url": "https://services9.arcgis.com/i2awudUyJK8gxxEe/arcgis/rest/services/Ionia_County_Tax_Parcels_Public/FeatureServer/0/query",
        "apn_field": "CountyParcels_PIN", "apn_prefix": "34-",
        "owner_field": "Parcel_Data_OWNERNAME",
        "mail_street_field": "Parcel_Data_OWNERADDRESS",
        "mail_city_field": "Parcel_Data_OWNERCITY",
        "mail_state_field": "Parcel_Data_OWNERSTATE",
        "mail_zip_field": "Parcel_Data_OWNERZIPCODE",
        "situs_street_field": "PROPERTYFULLADDR",
        "situs_city_field": "Parcel_Data_PROPERTYCITY",
        "situs_state_field": "Parcel_Data_PROPERTYSTATE",
        "situs_zip_field": "Parcel_Data_PROPERTYZIP", "verified": True,
        "note": "VERIFIED 2026-07-05 3/3; guard also catches the county's own TREASRUER misspelling."},
    "26103": {  # Marquette — PARTIAL: City of Ishpeming only (52-51-*); township rows stay blank.
        "url": "https://services5.arcgis.com/VxT1UTIFTgaEp7hM/arcgis/rest/services/Parcel_Master_File/FeatureServer/0/query",
        "apn_field": "PARCEL_ID", "owner_field": "OWNER1", "owner2_field": "OWNER2",
        "mail_street_field": "MAILING_AD", "mail_city_field": "MAILING_CI",
        "mail_state_field": "MAILING_ST", "mail_zip_field": "MAILING_ZI",
        "situs_street_field": "PROP_ADD", "situs_city_field": "PROP_CITY",
        "verified": True,
        "note": "VERIFIED 2026-07-05 1/3 (Ishpeming-only CUPPAD layer; 52-12-* absent — honest misses)."},
    "26147": {  # Saint Clair — VESTED (SCC TREASURER 3/3). Geocortex-backed county ArcGIS; PIN carries '74-'.
        "url": "https://maps.stclaircounty.org/GeoCortex_wa/rest/services/Public_GISweb/TaxParcels_Nightly/MapServer/0/query",
        "apn_field": "PARCEL_NUMBER", "apn_prefix": "74-",
        "owner_field": "OWNER", "owner2_field": "OWNER2",
        "mail_street_field": "OWNER_ADDRESS", "mail_city_field": "OWNER_CITY",
        "mail_state_field": "OWNER_STATE", "mail_zip_field": "OWNER_ZIP",
        "situs_street_field": "PROP_ADDRESS_COMBINED", "verified": True,
        "note": "VERIFIED 2026-07-05 3/3; vintage lane; TAXPAYER_* fields also exist for audit."},
    "26117": {  # Montcalm — VESTED (MONTCALM COUNTY TREASURER 3/3). BS&A-export schema.
        "url": "https://services6.arcgis.com/GJ2uZAPsEvtmqpe1/arcgis/rest/services/Tax_Parcels/FeatureServer/0/query",
        "apn_field": "TaxParcel_PARCELID", "owner_field": "BSAEXPORT_ONERNAME1",
        "owner2_field": "BSAEXPORT_ONERNAME2",
        "mail_street_field": "BSAEXPORT_OTADDRESS", "mail_city_field": "BSAEXPORT_OWNERCITY",
        "mail_state_field": "BSAEXPORT_ONERSTATE", "mail_zip_field": "BSAEXPORT_ORZIPCODE",
        "situs_street_field": "BSAEXPORT_PCOMBINED", "verified": True,
        "note": "VERIFIED 2026-07-05 3/3; APN dashed as-is; vintage lane."},
    # Van Buren County, MI (26159): NO FREE SOURCE (STEP-2d stop, 2026-07-01).
    # County portal is MangoMap (API tier returns 402/payment-disabled), BS&A
    # owner search sits behind a login wall, no county/AGOL REST found. The 29
    # cohort rows stay unenriched until an operator approves a paid tier or a
    # new free source appears.
    # St. Joseph County, IN (18141): county GIS is Beacon/Schneider-hosted —
    # no open ArcGIS REST endpoint known; tier 1 stays unavailable there and
    # the chain degrades straight to tier 2.
}


# ---- tier 1b: FetchGIS PSsearch2 counties (ENRICH-1 census, 2026-07-05) ---------
# County-official FetchGIS land-records search (app/providers/fetchgis.py).
# map = the FetchGIS map key; dash = 'true' (dash-separated PIN) / 'false'
# (space-separated); apn_strip_prefix = county code our canonical APN carries
# but the map omits. Owner name + combined situs only — NO owner mailing, NO
# geometry (honest NULL). Every row verified against >=1 real cohort APN.
# VESTED = treasurer/state roll (vintage lane).
FETCHGIS_ENDPOINTS = {
    "26107": {"map": "mecosta", "dash": "false", "verified": True,
              "note": "VESTED 3/3 'MICHIGAN DEPARTMENT OF TREASURY' (guard: TREASURY)."},
    "26025": {"map": "calhoun", "dash": "true", "verified": True,
              "note": "LIVE (BLACKOUT CONSTRUCTION LLC); Battle Creek 4-digit-first APNs likely miss (city roll absent from county LRP)."},
    "26137": {"map": "otsego", "dash": "true", "verified": True, "note": "VESTED 3/3 county treasurer."},
    "26149": {"map": "stjo", "dash": "false", "verified": True, "note": "VESTED 3/3 ST JOSEPH COUNTY TREASURER."},
    "26041": {"map": "delta", "dash": "true", "verified": True, "note": "LIVE 3/3 (BECKER CHRISTOPHER)."},
    "26033": {"map": "chip", "dash": "true", "verified": True, "note": "LIVE 2/2 (ENGLER WILLIAM JOSEPH)."},
    "26155": {"map": "shi", "dash": "true", "verified": True, "note": "LIVE 3/3 (JACKSON LATOYI M & DWAYNE A)."},
    "26151": {"map": "sanilac", "dash": "true", "verified": True, "note": "LIVE 2/2 (JACOBS GEORGE & ROSA)."},
    "26003": {"map": "alger", "dash": "true", "verified": True, "note": "LIVE 3/3 (KAHLSTORF CYNTHIA M)."},
    "26097": {"map": "mack", "dash": "true", "verified": True, "note": "VESTED 3/3 MACKINAC COUNTY TREASURER; map key is 'mack'."},
    "26153": {"map": "schoolcraft", "dash": "true", "verified": True, "note": "VESTED 3/3 county treasurer."},
    "26035": {"map": "clare", "dash": "true", "verified": True, "note": "LIVE 3/3 (LAINE KAREN)."},
    "26063": {"map": "huron", "dash": "true", "verified": True, "note": "VESTED 3/3 HURON COUNTY TREASURER."},
    "26129": {"map": "ogemaw", "dash": "true", "verified": True, "note": "LIVE 3/3 (YINGER TIMOTHY R)."},
    "26069": {"map": "iosco", "dash": "true", "verified": True, "note": "VESTED (STATE OF MICHIGAN on the 'Part of' parent parcel)."},
    "26127": {"map": "oceana", "dash": "true", "apn_strip_prefix": "64-", "verified": True,
              "note": "VESTED (STATE OF MICHIGAN); map omits our '64-' county code."},
    "26087": {"map": "lapeer", "dash": "true", "verified": True,
              "note": "LIVE (DE FELICE LUKE A ESTATE); PSsearch2 open even though the county's ArcGIS tier is WAF'd."},
    "26057": {"map": "gratiot", "dash": "true", "verified": True,
              "note": "VESTED sample ('GRATIOT COUNTY' — guard: trailing COUNTY); county AGOL layer is owner-stripped, this search is not."},
    "26073": {"map": "isabella", "dash": "true", "verified": True, "note": "VESTED sample ISABELLA COUNTY TREASURER."},
    "26037": {"map": "clinton", "dash": "true", "verified": True, "note": "LIVE (REEB, DORIS REVOC LIV TR)."},
}

# ---- tier 1c: Colligo GIS counties (ENRICH-1 census, 2026-07-05) ----------------
# County-official Colligo public links (app/providers/colligo.py). PUBLIC links
# only — login-gated Colligo counties (Menominee/Keweenaw/Ontonagon) stay
# uncovered. No geometry via these APIs (coords honest-NULL).
COLLIGO_ENDPOINTS = {
    "26079": {"mode": "findTab", "publink": "hYBhevUUbCBc9Sfv", "layer": "poly1622147747",
              "apn_key": "pid", "owner_field": "Owner",
              "mail_street_field": "OwnerAddress", "mail_city_field": "ownercity",
              "mail_state_field": "ownerstate", "mail_zip_field": "ownerzip",
              "situs_street_field": "PropAddress", "verified": True,
              "note": "Kalkaska. VESTED 3/3 'DEPARTMENT OF NATURAL RESOURCE' (guard covers DNR)."},
    "26061": {"mode": "findTab", "publink": "JWGuygu7GgdEHpHV", "layer": "poly1756926334",
              "apn_key": "pin", "owner_field": "ownername1", "owner2_field": "ownername2",
              "mail_street_field": "ownerstreetaddr",
              "situs_street_field": "propstreetcombined", "situs_city_field": "propcity",
              "situs_state_field": "propstate", "situs_zip_field": "propzip",
              "verified": True,
              "note": "Houghton. LIVE 3/3 (DAVIS JAMES H); OwnerCSZ combined city/st/zip unmapped."},
    "26053": {"mode": "findProps", "publink": "6JFA6Ft5hn34hnyQ", "layer": "poly1606773384",
              "apn_key": "pnum", "owner_field": "ownername1", "owner2_field": "ownername2",
              "mail_street_field": "ownerstreetaddr",
              "situs_street_field": "propstreetcombined", "situs_city_field": "propcity",
              "situs_state_field": "propstate", "situs_zip_field": "propzip",
              "verified": True,
              "note": "Gogebic. VESTED 3/3 county treasurer; findSomething->loadProps."},
    "26039": {"mode": "findTab", "publink": "S6wdeWC2y8VaRHj5", "layer": "poly1651664616",
              "apn_key": "pid", "owner_field": "Owner",
              "mail_street_field": "OwnerAddress", "mail_city_field": "ownercity",
              "mail_state_field": "ownerstate", "mail_zip_field": "ownerzip",
              "situs_street_field": "PropAddress", "verified": True,
              "note": "Crawford. VESTED 3/3 county treasurer; findTab re-verified 2026-07-05 "
                      "(the census agent's bulk-GeoJSON claim did NOT survive: int_loadData "
                      "serves geometry-only props, owner lives in findTabData)."},
}
