"""T4: parse an operator-pasted 'survived by' paragraph into heir candidates.
Stored as heir_candidate with confidence=obituary; never a resolved address."""
from __future__ import annotations

import re
from typing import Dict, List

RELATIONS = ("wife", "husband", "spouse", "son", "daughter", "children", "child", "brother", "sister",
             "mother", "father", "grandson", "granddaughter", "grandchildren", "grandchild", "niece", "nephew",
             "stepson", "stepdaughter", "partner", "companion", "sons", "daughters", "brothers", "sisters")
_REL_RE = re.compile(r"\b(" + "|".join(RELATIONS) + r")s?\b[,:]?\s*", re.IGNORECASE)
_NAME_RE = re.compile(r"([A-Z][a-zA-Z'\-\.]+(?:\s+(?:\(?[A-Z][a-zA-Z'\-\.]+\)?))*(?:\s+[A-Z][a-zA-Z'\-\.]+)*)")
_LOC_RE = re.compile(r"\bof\s+([A-Z][a-zA-Z\.]+(?:\s+[A-Z][a-zA-Z\.]+)*(?:,\s*[A-Z]{2})?)")


def parse_survived_by(text: str) -> List[Dict[str, str]]:
    """Return [{name, relationship, city, confidence}] from a paragraph such as
    'He is survived by his wife, Mary Smith of Raleigh; sons John Smith (Ann) of Cary and
    Paul Smith of Durham; and three grandchildren.' Counts-only phrases yield nothing."""
    out: List[Dict[str, str]] = []
    if not text:
        return out
    body = text.split("survived by", 1)[1] if "survived by" in text.lower() and re.search(r"survived by", text, re.I) else text
    body = re.split(r"survived by", body, flags=re.I)[-1]
    # split into clauses on ; or " and " between relation groups
    clauses = re.split(r";|\band\b(?=\s+(?:his|her|their|a|by|\w+\s+(?:wife|husband|son|daughter)))", body)
    current_rel = ""
    for clause in clauses:
        m = _REL_RE.search(clause)
        if m:
            current_rel = m.group(1).lower().rstrip("s") if m.group(1).lower() not in ("children", "grandchildren") else m.group(1).lower()
            clause = clause[m.end():]
        if not current_rel:
            continue
        # names: capitalised runs, skipping pronouns and count-only phrases
        for seg in re.split(r",\s*(?=[A-Z])|\band\b", clause):
            seg = seg.strip(" .,")
            if not seg or re.match(r"^(his|her|their|\d+|one|two|three|four|five|six|seven|eight|nine|ten|several|many|numerous)\b", seg, re.I):
                continue
            nm = _NAME_RE.match(seg)
            if not nm:
                continue
            name = re.sub(r"\s*\([^)]*\)", "", nm.group(1)).strip()
            if len(name.split()) < 2:
                continue
            loc = _LOC_RE.search(seg)
            out.append({"name": name, "relationship": current_rel, "city": (loc.group(1) if loc else ""),
                        "confidence": "obituary"})
    return out
