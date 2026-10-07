"""Is this ZIP possible for this state?

A ZIP that leaked from the mailing side does not stop being wrong because it is
five digits long. `809 MATTHEWS TOWNSHIP PY MATTHEWS NC` carried property_zip
`91201-5025`. 91201 is Glendale, California. Truncating it to five digits made
it look like a ZIP and left California on a Mecklenburg parcel.

USPS assigns the first three digits of a ZIP to a sectional centre facility, and
each facility serves exactly one state. So the state is decidable from the ZIP3
prefix, and the ranges below are the published assignment. This is a refusal
rule, never a repair rule: an implausible ZIP becomes UNKNOWN, never a guessed
one. UNKNOWN is a fact; a plausible-looking wrong ZIP is not.

A state we do not have ranges for cannot refuse anything -- the answer is "no
opinion", not "reject".
"""
from __future__ import annotations

from typing import Dict, List, Tuple

# state -> inclusive ZIP3 prefix ranges
ZIP3_RANGES: Dict[str, List[Tuple[int, int]]] = {
    "AL": [(350, 352), (354, 369)],
    "AK": [(995, 999)],
    "AZ": [(850, 853), (855, 857), (859, 860), (863, 865)],
    "AR": [(716, 729), (755, 755)],
    "CA": [(900, 908), (910, 928), (930, 961)],
    "CO": [(800, 816)],
    "CT": [(60, 69)],
    "DE": [(197, 199)],
    "DC": [(200, 205), (569, 569)],
    "FL": [(320, 339), (341, 342), (344, 344), (346, 347), (349, 349)],
    "GA": [(300, 319), (398, 399)],
    "HI": [(967, 968)],
    "ID": [(832, 838)],
    "IL": [(600, 620), (622, 629)],
    "IN": [(460, 479)],
    "IA": [(500, 528)],
    "KS": [(660, 679)],
    "KY": [(400, 427)],
    "LA": [(700, 701), (703, 708), (710, 714)],
    "ME": [(39, 49)],
    "MD": [(206, 219)],
    "MA": [(10, 27), (55, 55)],
    "MI": [(480, 499)],
    "MN": [(550, 567)],
    "MS": [(386, 397)],
    "MO": [(630, 658)],
    "MT": [(590, 599)],
    "NE": [(680, 693)],
    "NV": [(889, 898)],
    "NH": [(30, 38)],
    "NJ": [(70, 89)],
    "NM": [(870, 884)],
    "NY": [(5, 5), (63, 63), (100, 149)],
    "NC": [(270, 289)],
    "ND": [(580, 588)],
    "OH": [(430, 459)],
    "OK": [(730, 731), (734, 749)],
    "OR": [(970, 979)],
    "PA": [(150, 196)],
    "PR": [(6, 9)],
    "RI": [(28, 29)],
    "SC": [(290, 299)],
    "SD": [(570, 577)],
    "TN": [(370, 385)],
    "TX": [(733, 733), (750, 799), (885, 885)],
    "UT": [(840, 847)],
    "VT": [(50, 54), (56, 59)],
    "VA": [(201, 201), (220, 246)],
    "WA": [(980, 994)],
    "WV": [(247, 268)],
    "WI": [(530, 549)],
    "WY": [(820, 831)],
}


def zip_plausible_for_state(zip5: str, state: str) -> bool:
    """False only when we have ranges for the state and the ZIP is outside them."""
    z = (zip5 or "").strip()
    st = (state or "").strip().upper()
    if len(z) != 5 or not z.isdigit():
        return False
    ranges = ZIP3_RANGES.get(st)
    if not ranges:
        return True  # no opinion
    prefix = int(z[:3])
    return any(lo <= prefix <= hi for lo, hi in ranges)


def state_for_zip(zip5: str) -> str:
    """The single state a ZIP3 belongs to, or "" when it belongs to none/many."""
    z = (zip5 or "").strip()
    if len(z) != 5 or not z.isdigit():
        return ""
    prefix = int(z[:3])
    hits = [st for st, ranges in ZIP3_RANGES.items()
            if any(lo <= prefix <= hi for lo, hi in ranges)]
    return hits[0] if len(hits) == 1 else ""
