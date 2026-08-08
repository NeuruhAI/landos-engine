"""
scrub_conversion.py — retire the contamination in the CONVERSION lens (474 minted with
the spot-check gate skipped). AI-NEVER-DELETES: a contaminated row is RETIRED
(card_state='dead', stage='archived') with a durable, human-readable reason in
value_add_notes and a structured audit stamp in parcel_meta.retired — never deleted.

WHY value_add_notes / card_state / stage: sync_to_crm's discovered-tier UPDATE refreshes
signal_tags and parcel_meta (coalesce-overwrite) on every run, but it NEVER writes
card_state, stage, or value_add_notes. So the retirement + its human-readable reason
survive re-sync; the VA's call queue (active cards) stays clean permanently.

Classification (deterministic, order matters — first match wins):
  govt      — classify_owner_type()=='govt', joined parcel_owner.vintage_flag=='govt',
              or a govt-name pattern (the vintage-capture law: never pitch a treasurer).
  cemetery  — cemetery/burial/mausoleum/memorial-garden|park.
  school    — school/academy/education/college/university/seminary.
  medical   — hospital/health/medical/clinic/sanitarium.
  surname   — the church/lodge/temple/parish token is a SURNAME, not an institution:
              a clear personal signal (middle initial, '&' spouse join, generational
              suffix, or a known given name) AND no denominational/org word. Conservative
              by construction — a real church ("First Baptist Church", "Church of God")
              always carries an org word and is never retired. human_review=true is
              stamped so a person can reverse any that were wrong (retire, don't delete).
  keep      — a genuine church/lodge (the clean CONVERSION lens) OR an ambiguous row
              (flagged human_review, left ACTIVE — we do not guess it away).

    python -m scripts.scrub_conversion               # dry-run census + full eyeball (default)
    python -m scripts.scrub_conversion --commit      # retire the contaminated rows
    python -m scripts.scrub_conversion --spot-check  # 10-row rendered sheet on the SURVIVORS
"""
import argparse
import json
import re
from app.score_absentee import classify_owner_type

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
SCRUB_DATE = "2026-07-14"

# --- exclusion patterns (institutional but NOT a conversion target -> retire) ----------
GOVT_RE = re.compile(
    r"\b(state of|county of|city of|village of|township of|twp of| township| twp|"
    r"land ?bank|treasurer|department|dept of|natural resources|road commission|"
    r"drain commission|school district|public schools|board of education|"
    r"housing (commission|authority)|redevelopment authority|municipal|conservancy)\b", re.I)
# stem 'cemet' catches cemetery / cemetary / cemetry / cemeteries — the trailing \w* is what
# makes it robust to the 'Cemetary' misspelling a \b-terminated 'cemeter' would miss.
CEMETERY_RE = re.compile(r"\b(cemet\w*|burial|mausoleum|memorial\s+(garden|park|cemet\w*))\b", re.I)
SCHOOL_RE = re.compile(r"\b(school|academ|educat|college|universit|seminary|montessori|charter school)\b", re.I)
MEDICAL_RE = re.compile(r"\b(hospital|health|medical|clinic|sanitarium|nursing home)\b", re.I)

# The ambiguous religious/fraternal tokens that are ALSO common surnames. These are the
# ONLY tokens a surname-collision can hinge on; their mere presence proves nothing.
AMBIG_REL = {"CHURCH", "TEMPLE", "PARISH", "PARRISH", "LODGE", "CHAPEL", "GRANGE", "ABBEY"}

# Denominational / organizational / place-in-org-name words. Their presence marks a real
# institution and BLOCKS a surname-collision verdict (the row stays a legit church/lodge).
ORG_WORDS = {
    # denominations & religious descriptors
    "EVANGELICAL", "EVANG", "LUTHERAN", "LUTH", "LUTHERN", "METHODIST", "METH", "UMC",
    "BAPTIST", "BAPT", "CATHOLIC", "PRESBYTERIAN", "EPISCOPAL", "EPISCOLPAL", "PENTECOSTAL",
    "APOSTOLIC", "MENNONITE", "BRETHREN", "WESLEYAN", "ADVENTIST", "NAZARENE", "DISCIPLES",
    "GOSPEL", "BIBLE", "CHRISTIAN", "CRISTIANA", "IGLESIA", "JEHOVAH", "WITNESSES", "TRINITY",
    "BETHEL", "BETHANY", "ZION", "CALVARY", "EMMANUEL", "IMMANUEL", "SINAI", "DELIVERANCE",
    "REVIVAL", "LIGHTHOUSE", "CROSSROADS", "FREEDOM", "FREEWILL", "WORSHIP", "SANCTUARY",
    "FELLOWSHIP", "ASSEMBLY", "OUTREACH", "TABERNACLE", "CONGREGATION", "MINISTRIES",
    "MINISTRY", "MISSION", "MISSIONARY", "DIOCESE", "PARSONAGE", "SYNOD", "CONFERENCE",
    "SACRED", "HOLY", "SAINT", "CATHEDRAL", "REDEEMER", "SAVIOR", "SHEPHERD",
    # fraternal / veteran orgs
    "ELKS", "MOOSE", "MASONIC", "MASONS", "SHRINE", "SHRINER", "AERIE", "EAGLES", "LEGION",
    "VFW", "AMVETS", "VETERANS", "FRATERNAL", "BENEVOLENT", "ORDER", "LOYAL", "KNIGHTS",
    "POST", "AMERICAN",
    # generic org / civic / place-in-org tokens
    "INC", "LLC", "LTD", "CORP", "COMPANY", "CO", "ASSN", "ASSOCIATION", "SOCIETY",
    "TRUSTEES", "FOUNDATION", "CENTER", "CENTRE", "DISTRICT", "COUNCIL", "BOARD", "CLUB",
    "OF", "THE", "DE", "LA", "EL", "NORTH", "SOUTH", "EAST", "WEST", "CENTRAL", "FIRST",
    "SECOND", "THIRD", "NEW", "OLD", "GREATER", "UNITED", "COMMUNITY", "GERMAN", "INDIANA",
    "MICHIGAN", "MICH", "LAPORTE", "HUDSON", "MAPLE", "GROVE", "LAKE", "PRAIRIE", "VALLEY",
    "HABITAT", "SALVATION", "YMCA", "YWCA", "GOODWILL",
    # Saint / eastern-rite denominational markers ("Church St George Syrian" = an institution)
    "ST", "SYRIAN", "ORTHODOX", "GREEK", "SERBIAN", "COPTIC", "RUSSIAN", "ROMANIAN",
    "UKRAINIAN", "ANTIOCHIAN", "MARONITE", "MELKITE",
}
# "F & A M" / "A F & A M" — Free & Accepted Masons: a fraternal LODGE, not a person.
MASONIC_RE = re.compile(r"\bA?\.?\s*F\s*&\s*A\.?\s*M\b", re.I)
SUFFIX = {"JR", "SR", "II", "III", "IV", "V"}
# Given-name gazetteer — lets a bare "Surname Given" singleton (no initial/&/suffix) read
# as personal (the "Church Sandra" case). Observed in this lens + common given names.
GIVEN_NAMES = {
    "SANDRA", "VICKI", "VICKIE", "ROXANN", "ROXANNE", "DOUGLAS", "KELLY", "TERRY", "WENDY",
    "THOMAS", "TIMOTHY", "CATHI", "CATHY", "BOBBY", "CYNTHIA", "KENNETH", "PATRICK", "ARTHUR",
    "JAMES", "JOHN", "ROBERT", "MICHAEL", "WILLIAM", "DAVID", "RICHARD", "JOSEPH", "MARY",
    "PATRICIA", "LINDA", "BARBARA", "SUSAN", "JESSICA", "SARAH", "KAREN", "NANCY", "BETTY",
    "DONALD", "MARK", "PAUL", "STEVEN", "GARY", "KEVIN", "BRIAN", "RONALD", "JASON", "JEFFREY",
    "GEORGE", "EDWARD", "GREGORY", "LARRY", "SCOTT", "FRANK", "DENNIS", "JERRY", "DIANE",
    "JUDY", "JANET", "DEBRA", "SHIRLEY", "DONNA", "CAROL", "SHARON", "MICHELLE", "LAURA",
}


def _toks(name):
    return re.sub(r"[.,#/]", " ", (name or "").upper()).replace("&", " & ").split()


def classify(owner_name, vintage_flag):
    """Return (verdict, reason, human_review). verdict in {retire, keep}."""
    nm = (owner_name or "").strip()
    # 1) govt — the vintage-capture law (name pattern OR the stored/joined flag)
    if vintage_flag == "govt" or classify_owner_type(nm) == "govt" or GOVT_RE.search(nm):
        return ("retire", "govt", False)
    if CEMETERY_RE.search(nm):
        # a church-WITH-graveyard ("Church Bethany Evangelical (& Cemetary)") is ambiguous —
        # the church building may still be a conversion target, the graveyard is not. Retire
        # (a cemetery-encumbered parcel is not cleanly convertible) but flag it reversible.
        toks = _toks(nm)
        church_with_cemetery = any(t in ORG_WORDS for t in toks) or any(t in AMBIG_REL for t in toks)
        return ("retire", "cemetery", church_with_cemetery)
    if SCHOOL_RE.search(nm):
        return ("retire", "school", False)
    if MEDICAL_RE.search(nm):
        return ("retire", "medical", False)

    toks = _toks(nm)
    if not toks:
        return ("keep", "empty_name", True)
    # a real institution carries a denominational/org word -> keep (the clean lens)
    if any(t in ORG_WORDS for t in toks):
        return ("keep", "religious_org", False)
    # a numbered lodge/post (#222, "No 980", "Post 403") or the Masonic F&AM designation
    # is a fraternal INSTITUTION, never a person.
    if MASONIC_RE.search(nm) or any(t.isdigit() and len(t) >= 2 for t in toks):
        return ("keep", "religious_org", False)
    # "c/o <name>" with no org word — property held on behalf of something (a chapel via a
    # caretaker). Do NOT guess it into a homeowner; flag it, leave it active.
    if re.search(r"\bc/o\b", nm, re.I):
        return ("keep", "ambiguous_flagged", True)
    # the collision must hinge on an ambiguous-surname token actually being present
    if not any(t in AMBIG_REL for t in toks):
        return ("keep", "no_ambig_token", False)
    # personal signal in the NON-ambiguous remainder
    rest = [t for t in toks if t not in AMBIG_REL and t != "&"]
    has_initial = any(len(t) == 1 and t.isalpha() for t in rest)
    has_amp = "&" in toks
    has_suffix = any(t in SUFFIX for t in rest)
    has_given = any(t in GIVEN_NAMES for t in rest)
    if has_initial or has_amp or has_suffix or has_given:
        return ("retire", "surname_collision", True)   # reversible; flagged for a human
    # a lone ambiguous token ("Church Chapel", "Chapel Posey c/o ...") — do NOT guess it away
    return ("keep", "ambiguous_flagged", True)


def build_request(tenant_id: str = TENANT) -> dict[str, object]:
    """Return a non-executable request for the sole CRM writer."""
    return {
        "schema": "landos.crm_maintenance_request.v1",
        "action": "scrub_conversion",
        "tenant_id": tenant_id,
        "apply": False,
        "writer": "scripts/sync_to_crm.py",
        "authority_required": True,
        "classifier_version": SCRUB_DATE,
    }


def run(commit: bool = False) -> dict[str, object]:
    """Compatibility API: return a request, never execute the legacy mutation."""
    if commit:
        raise RuntimeError("commit removed: route the request through scripts.sync_to_crm")
    return build_request()


def spot_check(n: int = 10) -> dict[str, object]:
    request = build_request()
    request["sample_size"] = n
    return request


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="emit a read-only conversion scrub request")
    ap.add_argument("--commit", action="store_true", help="legacy spelling; always blocked")
    ap.add_argument("--spot-check", action="store_true", help="emit request only")
    ap.add_argument("--n", type=int, default=10, help="retained compatibility flag")
    a = ap.parse_args()
    if a.commit:
        ap.error("--commit removed: route a typed request through scripts.sync_to_crm")
    print(json.dumps(spot_check(a.n) if a.spot_check else run(), sort_keys=True))
    print("REQUEST ONLY — no database connection or CRM write.")
