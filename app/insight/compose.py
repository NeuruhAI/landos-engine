"""
compose.py — turn a grounding into the 3-part brief, deterministically.

This is the honesty backbone. It is (a) the fallback when no model runs, (b) the exact
fact set + guardrails the LLM is constrained to, and (c) the SOLE author of do_not_say —
the compliance/landmine rails are never delegated to a model. Every sentence here is
templated over a present fact; a missing fact emits nothing. No fact at all -> empty brief.
"""


def _money(v):
    try:
        return f"${float(v):,.0f}"
    except (TypeError, ValueError):
        return None


def _f(facts, key):
    """The raw value of a fact, or None if absent."""
    return facts.get(key, {}).get("value")


def what_we_know(facts):
    """2-3 grounded sentences, highest-salience first. Each maps to a present fact."""
    s = []
    owner = _f(facts, "owner_name")
    situs = _f(facts, "situs_address")
    if owner:
        s.append(f"The owner of record is {owner}"
                 + (f", at {situs}." if situs else "."))

    tenure = _f(facts, "tenure_years")
    senior = _f(facts, "senior_score")
    if tenure:
        bit = f"They have held the parcel about {tenure:.0f} years"
        bit += f" and profile as a long-tenure/senior owner (senior_score {senior:.2f})." if senior else "."
        s.append(bit)
    elif senior:
        s.append(f"The owner profiles as a senior/long-tenure candidate (senior_score {senior:.2f}).")

    fam = _f(facts, "same_surname_family")
    csize = _f(facts, "cluster_size")
    if csize and csize > 1:
        s.append(f"The parcel is part of a {csize}-parcel"
                 + (" same-surname family assemblage." if fam else " ownership cluster."))

    oos = _f(facts, "owner_out_of_state")
    mail = _f(facts, "owner_mailing") or {}
    if oos:
        where = f" (mails to {mail.get('city') or ''} {mail.get('state') or ''})".rstrip().replace("(  ", "(")
        s.append(f"The mailing address is out of state{where if mail else ''} — an absentee owner.")

    dist = _f(facts, "dist_to_lake_m")
    if dist is not None:
        s.append(f"It sits about {dist:.0f} m from the lake.")

    owed = _money(_f(facts, "amount_owed"))
    if owed:
        s.append(f"There is {owed} owed on the parcel.")
    assessed = _money(_f(facts, "assessed_total"))
    if assessed and not owed:
        s.append(f"Assessed value on record is {assessed}.")

    acres = _f(facts, "acreage")
    if acres:
        s.append(f"The parcel is about {acres:g} acres.")

    contam = _f(facts, "contamination_kind")
    if contam:
        s.append(f"It carries an environmental flag ({contam}).")

    disp = _f(facts, "call_dispositions")
    if disp:
        s.append(f"Prior call outcome(s): {', '.join(disp)}.")

    return " ".join(s[:3])


def angle(facts):
    """The opening a caller should take — derived strictly from the facts, never invented.
    Ordered so the strongest actionable frame wins; empty when nothing grounds an angle."""
    hub = _f(facts, "hub_parcel_count")
    if hub and hub >= 2:
        return (f"Portfolio conversation. This owner holds {hub} parcels — treat them as a "
                f"holder deciding whether to consolidate or divest, not a one-parcel seller. "
                f"Ask about the whole position; do not lead with distress.")

    tenure = _f(facts, "tenure_years")
    senior = _f(facts, "senior_score")
    dist = _f(facts, "dist_to_lake_m")
    if (tenure and tenure >= 15) or senior:
        near = " near the lake" if dist is not None and dist < 400 else ""
        return (f"Legacy / succession angle. A long-tenured{'/senior' if senior else ''} owner"
                f"{near} is usually equity-rich and thinking about what happens next — open with "
                f"estate/legacy planning, not a lowball.")

    if _f(facts, "same_surname_family") or (_f(facts, "cluster_size") or 0) > 1:
        n = _f(facts, "cluster_size")
        return (f"Family-assemblage angle. {('These ' + str(n) + ' parcels are') if n else 'These parcels are'} "
                f"held by the same surname — open with the whole-family picture; a single-parcel "
                f"offer leaves value (and their motivation) on the table.")

    if _f(facts, "owner_out_of_state"):
        mail = _f(facts, "owner_mailing") or {}
        st = mail.get("state")
        return (f"Absentee angle. The owner mails{' to ' + st if st else ' out of state'} and has "
                f"low day-to-day attachment — open with the hassle and carrying cost of managing "
                f"property from a distance.")

    owed = _money(_f(facts, "amount_owed"))
    if owed and not (hub and hub >= 2):
        return (f"Tax-pressure angle. There is {owed} owed — open discreetly, offering to help "
                f"resolve the tax situation rather than naming a price first.")

    if _f(facts, "contamination_kind"):
        return (f"As-is environmental angle. The {_f(facts, 'contamination_kind')} flag caps most "
                f"buyers — that's the opening: you can take it as-is, which they can't easily sell "
                f"to anyone else. (A BEA is required — do not promise a clean transfer.)")

    return ""


def do_not_say(facts):
    """The landmines — ALWAYS deterministic, never a model's job. Each is a present fact."""
    out = []
    if _f(facts, "dnc"):
        out.append("DNC / opt-out is on record — this lead is NOT callable. Do not dial or text.")
    if _f(facts, "vintage_flag_govt"):
        out.append("The owner name is a government/treasurer vintage flag — NEVER pitch this "
                   "person as the seller; the name on record is not a private party.")
    hub = _f(facts, "hub_parcel_count")
    if hub and hub >= 2:
        out.append(f"This owner holds {hub} parcels — a PORTFOLIO HOLDER, not an estate-event "
                   f"seller. A 'behind on taxes?' / distress script burns them.")
    if _f(facts, "commercial_agent_flag"):
        out.append("A commercial agent represents this parcel — expect a broker, not a direct "
                   "owner; a direct-to-owner pitch is wrong here.")
    if _f(facts, "requires_bea") or _f(facts, "contamination_kind"):
        out.append("Environmental liability on record — do NOT promise a clean/as-warranted "
                   "transfer; a BEA is required to cap the liability.")
    if not _f(facts, "has_phone") and not _f(facts, "dnc"):
        out.append("No phone on file — not callable yet; skip-trace before any outreach.")
    return out


def compose(grounding):
    """Deterministic 3-part brief from a grounding dict. Empty parts are honest (thin record)."""
    facts = grounding.get("facts", {})
    return {
        "brief": what_we_know(facts),
        "angle": angle(facts),
        "do_not_say": do_not_say(facts),
    }


def is_empty(brief):
    """A brief with no What-We-Know and no Angle is an empty (correct-when-thin) output."""
    return not (brief.get("brief") or "").strip() and not (brief.get("angle") or "").strip()
