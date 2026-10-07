"""Field-contamination court.

T20D found that `property_zip` was not the property's ZIP: it equalled
`mailing_zip` on 14,061 of 14,061 populated NC rows and agreed with the ZIP
actually written on the property line ZERO times. A column was trusted because
of its name.

That was found by hand, for one column. This module asks the same question of
every property/mailing field pair systematically, so the next instance is found
by the suite rather than by a lane. It reports; it does not repair. A detector
that silently rewrites data is a second source of the same defect.

Each check returns a Finding with the population it was measured over, so
"0 contaminated" and "0 measurable" can never be confused -- that distinction is
what makes a green result meaningful.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from collections import Counter
from typing import Callable, Dict, Iterable, List, Optional, Sequence

# The property ZIP is read with the SAME canonical helpers the matcher uses.
# Writing a second extractor here is how the court would reproduce the very
# defect it exists to catch: a naive five-digit scan reads the house number of
# "17104 NIBLICK LN" as a ZIP. Measured on the CURRENT estate, a naive scan
# "finds" a ZIP on 8,528 property lines; the canonical reader finds none,
# because these lines genuinely carry no ZIP.
from ..contact.address_normalize import extract_zip5, strip_leading_house

QUALITY_COURT_VERSION = "field_contamination_court.v2"

ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
STATE_RE = re.compile(r"\b([A-Z]{2})\b")
TRUST_MARKER_RE = re.compile(r"\b(TRUST|TRUSTEE|TR\b|LIVING TRUST|REVOCABLE)\b", re.I)
ENTITY_MARKER_RE = re.compile(r"\b(LLC|L\.L\.C|INC|INCORPORATED|CORP|CORPORATION|LP|LLP|COMPANY|CO)\b", re.I)

SEVERITY = ("CLEAN", "NOTE", "SUSPECT", "CONTAMINATED")


@dataclass
class Finding:
    check: str
    field: str
    compared_to: str
    severity: str
    measurable: int          # rows where the check could be evaluated at all
    affected: int            # rows failing the check
    rate: float
    detail: str
    sample: List[str]

    def to_dict(self) -> dict:
        return asdict(self)


def _norm(v: Optional[str]) -> str:
    return " ".join(str(v or "").strip().upper().split())


def _zip5(v: Optional[str]) -> str:
    m = ZIP_RE.search(str(v or ""))
    return m.group(1) if m else ""


def _rate(affected: int, measurable: int) -> float:
    return round(affected / measurable, 6) if measurable else 0.0


# Minimum population before a rate is allowed to mean anything.
MIN_MEASURABLE = 40


def _severity(rate: float, measurable: int) -> str:
    """Only TOTAL agreement over a real population is contamination.

    A property city equal to the mailing city is what owner-occupancy looks
    like, and 76% of CURRENT rows are exactly that. Calling it SUSPECT would
    bury the one column (property_zip) that agrees on 14,061 of 14,061 with no
    counterexample anywhere. The signal is the ABSENCE of disagreement, not its
    scarcity.
    """
    if measurable < MIN_MEASURABLE:
        return "CLEAN"
    if rate >= 1.0:
        return "CONTAMINATED"
    if rate >= 0.999:
        return "SUSPECT"
    return "CLEAN"


def _defect_severity(rate: float, measurable: int) -> str:
    """For checks where every hit is a genuine defect (bad APN, leaked marker),
    any nonzero rate matters.

    Below MIN_MEASURABLE a rate cannot separate systematic from incidental, so
    real hits are reported as SUSPECT rather than promoted to CONTAMINATED on a
    denominator of two.
    """
    if measurable == 0:
        return "CLEAN"
    if affected_is_zero := (rate == 0.0):
        return "CLEAN"
    if measurable < MIN_MEASURABLE:
        return "SUSPECT"
    return "CONTAMINATED" if rate >= 0.25 else "SUSPECT"


def _finding(check, field, compared_to, measurable, affected, detail, sample,
             defect_scale: bool = False) -> Finding:
    rate = _rate(affected, measurable)
    sev = (_defect_severity if defect_scale else _severity)(rate, measurable)
    return Finding(check, field, compared_to, sev,
                   measurable, affected, rate, detail, sample[:5])


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------

def check_side_mirroring(rows: Sequence[dict], prop_field: str, mail_field: str) -> Finding:
    """Does the property-side column simply mirror the mailing-side column?

    This is the exact shape of the T20D property_zip defect. Mirroring is only
    damning when the two sides genuinely differ for some rows in the population;
    an owner-occupied parcel legitimately has one address on both sides.
    """
    measurable = affected = 0
    sample: List[str] = []
    for r in rows:
        p, m = _norm(r.get(prop_field)), _norm(r.get(mail_field))
        if not p or not m:
            continue
        measurable += 1
        if p == m:
            affected += 1
        elif len(sample) < 5:
            sample.append(f"{prop_field}={p} != {mail_field}={m}")
    detail = (f"{affected}/{measurable} populated rows have {prop_field} identical to "
              f"{mail_field}. 100% identity means the property-side column is not "
              f"independently sourced.")
    return _finding("SIDE_MIRRORING", prop_field, mail_field, measurable, affected, detail, sample)


def zip_on_property_line(row: dict) -> str:
    """The ZIP actually written on the property line, house number removed."""
    return extract_zip5(
        strip_leading_house(row.get("property_address_raw")),
        strip_leading_house(row.get("property_address_normalized")),
    )


def check_against_property_line(rows: Sequence[dict], field: str = "property_zip") -> Finding:
    """Where the property ADDRESS LINE itself carries a ZIP, does the dedicated
    column agree with it?

    NOT MEASURABLE is a real and useful verdict here. On the CURRENT estate the
    property lines carry no ZIP at all, so this check has nothing to compare and
    says so, rather than manufacturing 2,442 false disagreements out of house
    numbers.
    """
    measurable = affected = 0
    sample: List[str] = []
    for r in rows:
        on_line = zip_on_property_line(r)
        col = _norm(r.get(field))
        if not on_line or not col:
            continue
        measurable += 1
        if on_line != col:
            affected += 1
            if len(sample) < 5:
                sample.append(f"line={on_line} col={col} :: {str(r.get('property_address_raw'))[:60]}")
    detail = (f"{affected}/{measurable} rows where the property line carries its own ZIP "
              f"disagree with column {field}."
              if measurable else
              f"NOT MEASURABLE: no property line in this estate carries a ZIP once the "
              f"house number is removed, so {field} cannot be checked against the line.")
    return _finding("COLUMN_VS_PROPERTY_LINE", field, "property_address_raw",
                    measurable, affected, detail, sample, defect_scale=True)


def check_marker_leakage(rows: Sequence[dict], owner_field: str,
                         person_fields: Sequence[str]) -> Finding:
    """A trust or entity marker appearing in a field that is supposed to hold a
    natural person is a leak: it means an organisation name is being carried as
    if it were a human."""
    measurable = affected = 0
    sample: List[str] = []
    for r in rows:
        for f in person_fields:
            v = _norm(r.get(f))
            if not v:
                continue
            measurable += 1
            if TRUST_MARKER_RE.search(v) or ENTITY_MARKER_RE.search(v):
                affected += 1
                if len(sample) < 5:
                    sample.append(f"{f}={v}")
    detail = (f"{affected}/{measurable} populated person-name values carry a trust or "
              f"entity marker.")
    return _finding("MARKER_LEAKAGE", ",".join(person_fields), owner_field,
                    measurable, affected, detail, sample, defect_scale=True)


# A county whose APNs genuinely come in two widths is not broken. Mecklenburg
# carries 14,144 eight-character APNs and 4 of nine -- and those four are
# "09110104B", an eight-character APN with a legitimate parcel suffix. The
# defect being hunted is a DROPPED LEADING ZERO, whose signature is an
# all-numeric value SHORTER than the county's dominant width.
APN_DOMINANCE = 0.90


def check_apn_shape(rows: Sequence[dict], field: str = "apn") -> Finding:
    widths: Dict[str, Counter] = {}
    measurable = 0
    for r in rows:
        v = str(r.get(field) or "").strip()
        if not v:
            continue
        measurable += 1
        widths.setdefault(_norm(r.get("county")), Counter())[len(v)] += 1

    affected = 0
    sample: List[str] = []
    dominant: Dict[str, int] = {}
    for county, counter in widths.items():
        total = sum(counter.values())
        width, n = counter.most_common(1)[0]
        dominant[county] = width
        if total < MIN_MEASURABLE or n / total < APN_DOMINANCE:
            # No single width dominates: this county's scheme is genuinely mixed
            # (Johnston, Union, Wake all are). Nothing to conclude.
            continue
        for r in rows:
            if _norm(r.get("county")) != county:
                continue
            v = str(r.get(field) or "").strip()
            if v and len(v) < width and v.isdigit():
                affected += 1
                if len(sample) < 5:
                    sample.append(f"{county} apn={v} len={len(v)} dominant={width}")
    shown = ", ".join(f"{c}:{dominant[c]}({dict(sorted(widths[c].items()))})"
                      for c in sorted(widths))
    detail = (f"{affected}/{measurable} APNs are all-numeric and SHORTER than their "
              f"county's dominant width, the signature of a dropped leading zero. "
              f"Longer values with a parcel-suffix letter are legitimate and are not "
              f"counted. Dominant width per county: {{{shown}}}")
    return _finding("APN_SHAPE", field, "county", measurable, affected, detail, sample,
                    defect_scale=True)


def check_duplicate_owner_fields(rows: Sequence[dict], fields: Sequence[str]) -> Finding:
    """Two owner columns holding the same string means one of them is not
    carrying the information its name promises."""
    measurable = affected = 0
    sample: List[str] = []
    present = [f for f in fields if any(r.get(f) for r in rows)]
    if len(present) < 2:
        return _finding("DUPLICATE_OWNER_FIELDS", ",".join(fields), "", 0, 0,
                        "fewer than two owner columns are populated; not measurable", [])
    for r in rows:
        vals = [_norm(r.get(f)) for f in present]
        vals = [v for v in vals if v]
        if len(vals) < 2:
            continue
        measurable += 1
        if len(set(vals)) == 1:
            affected += 1
            if len(sample) < 5:
                sample.append(f"{present}={vals[0]}")
    detail = f"{affected}/{measurable} rows carry an identical value in every populated owner column."
    return _finding("DUPLICATE_OWNER_FIELDS", ",".join(present), "", measurable, affected,
                    detail, sample, defect_scale=True)


def check_state_plausibility(rows: Sequence[dict], field: str, expected: str) -> Finding:
    """The property state of an NC parcel is NC. A mailing state may be
    anywhere -- that is absentee ownership, not a defect. Running this on the
    property side only is the point."""
    measurable = affected = 0
    sample: List[str] = []
    for r in rows:
        v = _norm(r.get(field))
        if not v:
            continue
        measurable += 1
        if v != expected:
            affected += 1
            if len(sample) < 5:
                sample.append(f"{field}={v} expected={expected} apn={r.get('apn')}")
    detail = f"{affected}/{measurable} rows have {field} != {expected}."
    return _finding("STATE_PLAUSIBILITY", field, expected, measurable, affected, detail,
                    sample, defect_scale=True)


def run_quality_court(rows: Sequence[dict]) -> List[Finding]:
    """Every systematic field check, run over the CURRENT estate."""
    findings: List[Finding] = [
        check_side_mirroring(rows, "property_zip", "mailing_zip"),
        check_side_mirroring(rows, "property_city", "mailing_city"),
        check_side_mirroring(rows, "property_state", "mailing_state"),
        check_side_mirroring(rows, "property_address_raw", "mailing_address"),
        check_against_property_line(rows, "property_zip"),
        check_state_plausibility(rows, "property_state", "NC"),
        check_apn_shape(rows),
        check_duplicate_owner_fields(rows, ["owner_name", "owner_name_2", "owner_full_name"]),
        check_marker_leakage(rows, "owner_name", ["matched_person", "heir_person_id"]),
    ]
    return findings


QUALITY_COLUMNS = ["check", "field", "compared_to", "severity", "measurable",
                   "affected", "rate", "detail", "sample"]
