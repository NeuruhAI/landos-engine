# VA RUNBOOK — Contact Resolution (D1 v2, E1 human-fed)

You are turning a ranked list of property owners into a **verified callable list**.
You work the CSV top-down (highest score first), resolve each owner to a real
phone number using **free public sources**, score your confidence honestly, and
hand the file back. The engine imports your work, builds the kin graph, and
projects callable rows to the dialer.

**The one rule: never invent a contact.** A blank cell is good data ("free
sources missed") — it routes the row to the paid backstop. A guessed phone
number poisons the list and wastes dialing time. When unsure, write your doubt
in `va_notes` and move on.

---

## 1 — The files

| File | What it is | What you do |
|---|---|---|
| `exports/callable_prep_individuals_and_entities_119_<date>.csv` | The working file: 38 individuals (SOP-1) + 81 entities (SOP-2), ranked by score | Resolve every row |
| `exports/callable_review_govt_17_<date>.csv` | 17 government-owned parcels | Do NOT dial-resolve; see SOP-3 |

- Open in Google Sheets / Excel / Numbers. **Never edit `lead_id`, never reorder
  or delete columns, never re-sort without keeping whole rows intact.**
- You fill ONLY: `phone_1..phone_2_*`, `relative_1/2_*`, `email_1`,
  `verified_via`, `resolved_at`, `va_notes`, `dnc_status` (only via scrub),
  `outreach_recipe`, `primary_target`.
- Hand back as CSV named the same + `_RESOLVED` (e.g.
  `callable_prep_individuals_and_entities_119_2026-06-09_RESOLVED.csv`).
- `sop_path` tells you which branch to run: `SOP-1-direct` | `SOP-2-pierce`.

## 2 — The recipes (stamp as your LAST step per row)

The recipe records *who we should actually be talking to and why* — it is
decided by **liveness + ownership form**, which only you (the resolver) learn.

| Recipe | When | `primary_target` |
|---|---|---|
| **R1** | Living individual owner, direct line found | `owner` |
| **R2** | Entity (LLC/corp) pierced to its controlling member/manager | `member` |
| **R3** | Trust — pierced via deed/grantor to the trustee | `trustee` |
| **R4** | Owner deceased — contact pivots to heirs; if probate is open, the court-appointed personal representative | `heirs` (or `PR` when one is appointed) |
| **R5** | Government / land-bank owned — official channels only, never cold-dialed | `owner` (the public body) |

(Canon: **PR is a target INSIDE R4**, not its own recipe — a probate-opened
estate stays R4 with `primary_target=PR`. **R5 is reserved for
government/land-bank rows** — the SOP-3 review file.)

## 3 — SOP-1: PERSON (38 rows, `sop_path=SOP-1-direct`)

Owner names in parcel data are usually **SURNAME FIRST MIDDLE** — flip them
("PERSON CANTRICE T" → search "Cantrice Person").

1. **Search free people-search** — TruePeopleSearch.com, FastPeopleSearch.com,
   ThatsThem.com — by flipped name + mailing city/state from the CSV. These
   sites are Cloudflare-gated against robots; you, a human in a browser, pass
   free. (That is exactly why this step is yours.)
2. **Disambiguate** — the right record shows an address matching the CSV's
   `owner_mailing_address` (or the `situs_address`), and a plausible age.
   Common name + no address overlap = do NOT pick one; note ambiguity, move on.
3. **Liveness check — the R1-vs-R4 fork.** Deceased banner on the record, or a
   quick obit search (Legacy.com, FindAGrave.com — browser only, both block
   robots) for name + city. **If deceased → jump to SOP-1b (heirs).**
4. **Extract** — phones in order **mobile > landline > VOIP** into
   `phone_1`/`phone_2` (+ type, + source site), email into `email_1`,
   relatives/household members into `relative_1/2_name` (+ phone if shown).
5. **Cross-verify** — find the SAME number on a **second independent source** →
   note both in `verified_via` (e.g. `truepeoplesearch,thatsthem`). Two-source
   agreement is the single biggest confidence raiser.
6. **Score** (rubric §6), stamp `outreach_recipe=R1`, `primary_target=owner`,
   `resolved_at=<today YYYY-MM-DD>`.

### SOP-1b: deceased → heirs (R4)

1. From the obit: list survivors (spouse, children) — these are the heir chain.
2. Put the two best-reachable heirs in `relative_1/2_name` + phones (run SOP-1
   on each heir), and write `HEIR:` before their names in `va_notes` — the
   importer turns these into kin-graph rows (`is_heir=true`).
3. If probate/estate case is findable (county probate index) and a personal
   representative is named → stay `outreach_recipe=R4`, set
   `primary_target=PR`, PR's name/phone in `relative_1` +
   `va_notes=PR per <county> probate <case#>`.
4. Otherwise `outreach_recipe=R4`, `primary_target=heirs`.

## 4 — SOP-2: ENTITY pierce (81 rows, `sop_path=SOP-2-pierce`)

Goal: find the **controlling human** behind the name, then run SOP-1 on them.

1. **Pick the registry by county:**
   - `county_fips=26021` (Berrien, MI) → **MiBusiness Registry Portal**
     `https://mibusinessregistry.lara.state.mi.us/search/business`
     (⚠ this REPLACED the old LARA COFS in June 2025 — the old
     `cofs.lara.state.mi.us` host is dead; brief Cloudflare "Just a moment"
     screen clears by itself)
   - `county_fips=18141` (St. Joseph, IN) → **INBiz public search**
     `https://bsd.sos.in.gov/publicbusinesssearch`
2. **Search the entity name.** Try without suffix too ("ALUTECH" not
   "ALUTECH LLC"). The MI portal matches **prior names** — a hit may show a
   different current name (that is a finding, not a mismatch; note it).
3. **No hit? → foreign entity.** Out-of-state mailing address tells you where
   it actually lives — search THAT state's registry (e.g. CA →
   `https://bizfileonline.sos.ca.gov/search/business`). Note
   `foreign entity — no MI/IN registration` in `va_notes`.
4. **From the record pull:** registered agent, registered office street +
   mailing addresses, management type, status, entity ID, prior names.
5. **Commercial agent? FLAG AND SKIP.** If the agent is CT Corporation /
   The Corporation Company / CSC / Northwest Registered Agent / Registered
   Agents Inc / InCorp / Cogency / Harvard Business Services — that is a paid
   service, NOT the owner. Find the real principal instead:
   - registered-office **mailing** address (often the parent company's HQ —
     cross-check it against the CSV's `owner_mailing_address`; an exact match
     between tax roll and registry is near-proof),
   - the **Annual Report/Statement PDF** in the registry's History & Filings
     (often names the manager/members),
   - the **deed**: county Register of Deeds grantor/grantee index — who signed.
6. **Trusts have no registry** ("... TRUST", "... REVOCABLE LIVING TRUST"):
   pierce via the mailing address (reverse-search it on the people-search
   sites — who lives there = likely trustee/grantor) or the Register of Deeds
   (deed INTO the trust names the grantor; often trustee too). →
   `outreach_recipe=R3`, `primary_target=trustee`.
7. **Write the trail** in `va_notes` (registry, entity ID, agent + commercial
   flag, principal address, who the controlling human is and HOW you know),
   then **run SOP-1 on the controlling human** for phones.
8. Stamp `outreach_recipe=R2` (or R3 trust), `primary_target=member` (or
   `trustee`).

## 5 — SOP-3: GOVT (17 rows, separate review file)

Cities, school districts, road commissions. **Never cold-dial these.** No
people-search, no piercing. If the operator wants them pursued: the contact is
the relevant office (assessor/treasurer/economic development) via the official
.gov website's published line — official-channels outreach (letter/meeting),
not the dialer. These rows are recipe **R5** (`outreach_recipe=R5`,
`primary_target=owner` — the public body itself); note the responsible office
in `va_notes` if you look it up.

## 6 — Confidence rubric (write a number 0–1 in `phone_X_confidence`)

Start from **0.4** = one plausible source record (name + city match). Then:

| Signal | Adjust |
|---|---|
| Exact address match (mailing or situs) on the source record | **+0.2** |
| Same number on a 2nd independent source (`verified_2x`) | **+0.2** |
| Line type = mobile | **+0.1** |
| Relative/household corroboration, or age consistent | **+0.1** |
| Name matches deed signer / tax-roll owner exactly | **+0.1** |
| Data clearly old (last-seen > ~3 yrs) | **−0.2** |
| Common-name ambiguity not fully resolved | **−0.2** |
| VOIP or "disconnected" flag | **−0.3** |
| Address mismatch | **−0.2** |
| Entity where you ONLY have the commercial agent (no pierce) | cap at **0.3** |

Clamp to 0–1. Worked arithmetic: one source + exact mailing-address match +
second source agrees + mobile = 0.4+0.2+0.2+0.1 = **0.9**.

(Numbers are the operator's starting calibration — they get tuned after the
blind-50 validation.)

## 7 — What "callable" means (the QA gate)

A row becomes **callable** only with **≥1 phone at confidence ≥ 0.6 from ≥1
verified source** (`verified_via` non-empty). Below that, leave what you found —
the engine routes it to the paid backstop (Tracerfy) later. Rows with nothing:
leave contact columns blank, `va_notes=free sources: no hit`.

## 8 — DNC: not your call to skip (compliance gate)

- `dnc_status` ships as `unchecked`. **A row is NOT dialed until it reads
  `clear`.**
- The scrub (against the National DNC Registry + any internal suppression list)
  happens **immediately before a dial session — never at enrichment time**
  (numbers age; a clear from last month is stale).
- You only set `dnc_status` if the operator has given you the scrub tool;
  otherwise leave `unchecked` and the operator scrubs before dialing.
- All dialing is manual (no autodialer), 8am–9pm in the lead's local time —
  that is the dialer's burden, but never mark a row in a way that hides it.

---

## 9 — WORKED EXAMPLE A (R2 entity pierce) — already done, in the database

**Row:** `ALUTECH LLC` — Berrien (26021), mailing Southfield MI. Score 72.02.

What the engine found on 2026-06-09 via the MI registry (single public lookup):

1. Searched `ALUTECH LLC` on `mibusinessregistry.lara.state.mi.us` → 1 hit,
   matched as **prior name**: current entity is **ALUDYNE WEST MICHIGAN LLC**
   (Domestic LLC, ID 801249741, Active / AR Good, filed 10/14/2003,
   manager-managed).
2. Resident agent: **THE CORPORATION COMPANY** → **commercial agent (CT Corp
   family) — flagged, skipped** per SOP-2 step 5.
3. Registered-office mailing: **300 Galleria Officentre Ste 501, Southfield MI
   48034** — which is **Aludyne, Inc.'s HQ**, and is **byte-for-byte the
   lead's own tax-roll mailing address** in the CSV. Two independent public
   sources (county tax roll + state registry) point at the same controlling
   parent: **Aludyne, Inc.**
4. Written to the row's `skip_trace_meta`: `controlling_human = Aludyne, Inc.
   (corporate parent, role=manager)`, `outreach_recipe=R2`,
   `primary_target=member`, full registry record + provenance.
5. **Remaining VA work on this row:** the named individual manager is in the
   Annual Statement PDF (registry → History & Filings → Download) if the
   operator wants a person; otherwise resolve Aludyne corporate real-estate
   line (company site / LinkedIn) → fill phones → score.

**Foreign-entity branch, live example:** the #2-ranked entity
`SKYLINE REAL ESTATE SERVICE LLC` (mailing Coronado, CA) has **NO Michigan
registration** (searched 2026-06-09, zero results) — it is a foreign LLC that
never qualified in MI. Per SOP-2 step 3 the VA searches **California**:
`bizfileonline.sos.ca.gov` → "SKYLINE REAL ESTATE SERVICE". Note the miss in
`va_notes` exactly like that.

## 10 — WORKED TEMPLATE B (R1 person) — VA runs this

**Row:** `PERSON CANTRICE T` — Berrien (26021), mailing **Berwyn, IL**.
Score 90.91 (top individual). Surname-first → the human is **Cantrice T
Person** (distinctive given name = easy disambiguation).

1. TruePeopleSearch → Name: `Cantrice Person`, City/State: `Berwyn, IL`.
2. Right record must show the CSV's `owner_mailing_address` (Berwyn) in
   current/past addresses. Distinctive name → expect exactly one candidate;
   if several, address decides.
3. Liveness: no deceased flag expected; if one appears → SOP-1b.
4. Extract best mobile → `phone_1` (`phone_1_type=mobile`,
   `phone_1_source=truepeoplesearch`), second number → `phone_2`; email →
   `email_1`; two strongest relatives (same surname, Berwyn/Chicago area) →
   `relative_1/2`.
5. Confirm `phone_1` on FastPeopleSearch or ThatsThem →
   `verified_via=truepeoplesearch,fastpeoplesearch`.
6. Score (likely 0.4+0.2+0.2+0.1=0.9 if all four signals land), stamp
   `outreach_recipe=R1`, `primary_target=owner`, `resolved_at`, done.

## 11 — WORKED TEMPLATE C (R4 liveness→heirs) — VA runs this

**Row:** `BOERMA FAMILY REAL ESTATE` — Berrien (26021). Classified
"individual" but it is a **family-business umbrella name** — the resolution IS
the liveness/heir workflow: find which Boerma controls (or controlled) it.

1. **Deed first** (this name won't be in people-search): Berrien County
   Register of Deeds index search, grantor/grantee = `BOERMA FAMILY REAL
   ESTATE` → the deed shows who signed (e.g. "John Boerma, as ...") — that
   person is the controlling human. Note book/page in `va_notes`.
2. **Liveness on the deed signer:** TruePeopleSearch (name + Berrien-area
   city) deceased flag; obit search Legacy.com + FindAGrave + the local paper
   (Herald-Palladium) for `<name> Boerma`.
   ⚠ Legacy/FindAGrave **block robots (verified 2026-06-09: HTTP 403 to
   scripts)** — browser-only, which is you.
3. **If living** → run SOP-1 on them → `R1`, `primary_target=owner` (note
   "controlling human of family business" in `va_notes`).
4. **If deceased** → obit survivors = heir chain → SOP-1 each heir for phones →
   `relative_1/2` with `HEIR:` prefix in `va_notes`; check Berrien probate
   index — PR appointed? → `R4` with `primary_target=PR`; else `R4`/`heirs`.
5. Score heirs' phones with the same rubric (address overlap = heir's own
   address vs people-search record).

---

## 12 — Source boundaries (why which tool)

- **People-search + obit sites** (TruePeopleSearch, FastPeopleSearch,
  ThatsThem, Legacy, FindAGrave): Cloudflare/bot-gated; free for a human in a
  browser. **Manual = you.** Engine never scrapes them (E1 doctrine).
- **State business registries + Register of Deeds + probate indexes**: public
  lookup services, automatable, single queries — the engine may pre-pierce
  rows for you (like Worked Example A) and will say so in the CSV/`va_notes`.
- **Paid data** (Tracerfy backstop): the ENGINE's job, only on rows you mark
  below threshold, only when the operator arms the key. Never buy data
  yourself.
