-- 033_mine_exempt_structural_discriminator.sql
-- Fix landos.v_mine_exempt_institutional (defined 028): make it discriminate an
-- INSTITUTION from a FAMILY whose surname is Church/Temple/Parish/Lodge.
--
-- WHY (ccT4 proof): the 028 view flagged CONVERSION by an owner-name TRIGGER regex
-- alone (church|lodge|temple|…) OR vintage_flag='govt'. property_class does NOT
-- discriminate — real churches AND the "Church" family both sit at 529/530, only 16
-- of 474 carry an exempt class. So a homeowner "Church Sandra" / "Temple Patrick &
-- Roxann" was swept in as 'religious'. 26 such families were minted into the CONVERSION
-- lens and later retired (scripts/scrub_conversion --commit). This view is the lens's
-- DECLARATIVE definition; it must carry the same discriminator so it never lies again.
--
-- THE STRUCTURAL DISCRIMINATOR (name STRUCTURE, not property_class, not a roll):
--   * An INSTITUTION carries a denomination / fraternal / civic QUALIFIER token —
--     "First Baptist Church", "Church Of God In Christ", "Elks Lodge 233",
--     "American Legion Post 403". has_org_token is structural proof of an institution.
--   * A FAMILY is the ambiguous trigger surname + a PERSONAL structure and NO qualifier
--     — a given name ("Church Sandra"), a middle initial ("Church Douglas G"), an &
--     spouse join ("Temple Patrick & Roxann"), or a generational suffix. -> surname_review.
--   * Ambiguous (a trigger, but neither a qualifier nor a personal signal — "Church
--     Chapel") -> review. It is NOT guessed either way.
-- institution_kind gains the honest buckets {surname_review, review, cemetery, school,
-- medical} alongside the mintable {religious, fraternal_lodge, veterans, grange, govt}.
-- A new boolean `mintable` is TRUE only for a proven institution that is not govt/
-- cemetery/school/medical. surname_review + review are mintable=false: they DO NOT MINT.
--
-- 🔴 CANON — READ BEFORE RUNNING THIS ON A NEW COUNTY:
--   This lens is REGEX-DERIVED, NOT exempt-roll-derived. `mintable` is a STRUCTURAL
--   GUESS from the owner-name string — there is NO authoritative exempt/tax-exempt
--   source behind it. It was tuned on LaPorte (18091). Pointed at Berrien's ~86k
--   parcels (or any county without a real exempt roll) it WILL both miss institutions
--   whose names carry no qualifier ("Sauktown Church") and, on a genuinely ambiguous
--   name, guess wrong. Everything uncertain is therefore routed to review/surname_review
--   and DOES NOT MINT — safety over yield, by design. DO NOT relax `mintable` or bulk-mint
--   this view on a new county without (a) a real exempt-property source to join, or
--   (b) the mandatory human spot-check gate (scripts/mint_discovery --spot-check).
--   The mint path (scripts/mint_discovery mine_exempt) enforces the SAME has_org_token
--   requirement — keep the two in lockstep if either regex changes.
--
-- Read-only; additive (adds `mintable`, refines institution_kind values — the view is
-- referenced only by its own definition, no consumer depends on the old value set).
-- Reversible: 028 holds the prior definition verbatim.

create or replace view landos.v_mine_exempt_institutional as
with base as (
  select
    po.county_fips, po.parcel_apn, po.owner_name, po.property_class,
    po.vintage_flag, po.acres,
    -- exclusion categories (a conversion target is none of these) --------------
    (po.vintage_flag = 'govt'
     or po.owner_name ~* '\y(state of|county of|city of|village of|township of|twp of|town of|land ?bank|treasurer|natural resources|road commission|drain commission|school district|public schools|board of education|housing (commission|authority)|redevelopment authority|municipal|conservancy|sanitary district|drainage board)\y'
    ) as is_govt,
    -- NOTE the correct cemetery pattern: `cemeter|cemetar` (not `\ycemeter\y`, which
    -- fails on "Cemetery" — no boundary between 'r' and 'y'). A church that merely OWNS
    -- a cemetery still carries a denomination token and is caught as religious first only
    -- if it lacks a cemetery word; when both appear we route to review (conservative).
    (po.owner_name ~* '(cemeter|cemetar|\yburial\y|\ymausoleum\y|memorial (garden|park))') as is_cemetery,
    (po.owner_name ~* '\y(school|academ|educat|college|universit|seminary|montessori)\y') as is_school,
    (po.owner_name ~* '\y(hospital|health|medical|clinic|sanitarium|nursing home)\y') as is_medical,
    -- trigger families (the ambiguous surname tokens live in the first group) --------
    (po.owner_name ~* '\y(church|chapel|parish|ministr|congregation|temple|mosque|synagogue|diocese|tabernacle|worship)\y') as trig_relig,
    (po.owner_name ~* '\y(lodge|elks|moose|masonic|odd ?fellows|knights of|shriner|eagles aerie)\y') as trig_fratern,
    (po.owner_name ~* '\y(vfw|veterans of foreign|american legion|amvets|legion post)\y') as trig_vet,
    (po.owner_name ~* '\ygrange\y') as trig_grange,
    -- STRUCTURAL PROOF OF AN INSTITUTION: a denomination / fraternal / civic / org
    -- QUALIFIER token, OR a numbered lodge/post, OR the Masonic F&AM designation. Its
    -- ABSENCE on an ambiguous surname trigger is what marks a family. (Deliberately
    -- excludes bare place words like 'lake'/'grove' — too common in surnames; a
    -- "<Place> Church" with no other qualifier lands in review, not auto-mint.)
    (po.owner_name ~* '\y(baptist|methodist|meth|umc|lutheran|luth|luthern|catholic|presbyterian|pentecostal|pentacostal|apostolic|mennonite|brethren|wesleyan|adventist|nazarene|disciples|gospel|bible|christian|cristiana|iglesia|jehovah|witnesses|trinity|calvary|emmanuel|immanuel|sinai|deliverance|revival|lighthouse|crossroads|freewill|sanctuary|fellowship|assembly|outreach|congregation|ministr|mission|missionary|diocese|parsonage|synod|conference|sacred|holy|cathedral|redeemer|savior|shepherd|evangelical|evang|episcopal|orthodox|reformed|covenant|foursquare|elks|moose|masonic|masons|shrine|shriner|aerie|eagles|legion|vfw|amvets|veterans|fraternal|benevolent|loyal|knights|inc|llc|ltd|corp|company|assn|association|society|trustees|foundation|center|centre|district|council|club|habitat|salvation|ymca|ywca|goodwill|first|second|third|greater|united|community|of|syrian|greek|serbian|coptic|russian|romanian|ukrainian|antiochian|maronite|melkite)\y'
     or po.owner_name ~* '(#\s*[0-9]{1,5}\y|\y(no|post)\s+[0-9]{1,5}\y|\y[0-9]{2,5}\y)'
     or po.owner_name ~* '\ya?\.?\s*f\s*&\s*a\.?\s*m\y'
    ) as has_org_token,
    -- PERSONAL structure: a lone middle initial, an & spouse join, a generational
    -- suffix, or a common given name following the surname trigger.
    (po.owner_name ~* '\y[a-z]\y'
     or position('&' in po.owner_name) > 0
     or po.owner_name ~* '\y(jr|sr|ii|iii|iv)\y'
     or po.owner_name ~* '\y(james|john|robert|michael|william|david|richard|joseph|thomas|charles|christopher|daniel|matthew|anthony|donald|mark|paul|steven|andrew|kenneth|joshua|kevin|brian|george|edward|ronald|timothy|jason|jeffrey|ryan|jacob|gary|nicholas|eric|jonathan|stephen|larry|justin|scott|brandon|frank|benjamin|gregory|samuel|raymond|patrick|jack|dennis|jerry|douglas|peter|carl|arthur|roger|joe|terry|gerald|keith|willie|ralph|roy|bruce|adam|harry|fred|wayne|billy|steve|louis|jeremy|randy|howard|eugene|russell|bobby|victor|martin|ernest|phillip|todd|jesse|craig|alan|shane|earl|jimmy|danny|bryan|tony|mike|stanley|leonard|nathan|dale|rodney|curtis|norman|allen|marvin|vincent|glenn|travis|jeff|chad|melvin|wendell|mary|patricia|jennifer|linda|elizabeth|barbara|susan|jessica|sarah|karen|nancy|margaret|betty|sandra|dorothy|kimberly|emily|donna|michelle|carol|amanda|melissa|deborah|stephanie|rebecca|sharon|laura|cynthia|kathleen|amy|angela|shirley|brenda|pamela|nicole|katherine|virginia|catherine|helen|debra|rachel|carolyn|janet|maria|heather|diane|julie|joyce|victoria|kelly|christina|joan|evelyn|lauren|judith|megan|cheryl|andrea|hannah|martha|gloria|teresa|ann|frances|janice|jean|judy|denise|marilyn|danielle|beverly|theresa|diana|natalie|brittany|marie|lori|vicki|vickie|cathi|cathy|wendy|roxann|roxanne)\y'
    ) as has_personal_struct
  from landos.parcel_owner po
  where po.vintage_flag = 'govt'
     or po.owner_name ~* '\y(church|chapel|parish|ministr|congregation|temple|mosque|synagogue|diocese|tabernacle|worship|lodge|elks|moose|masonic|odd ?fellows|knights of|shriner|eagles aerie|vfw|veterans of foreign|american legion|amvets|legion post|grange)\y'
),
classified as (
  select b.*,
    case
      when is_govt      then 'govt'
      when is_cemetery  then 'cemetery'
      when is_school    then 'school'
      when is_medical   then 'medical'
      when has_org_token then
        case when trig_vet     then 'veterans'
             when trig_fratern then 'fraternal_lodge'
             when trig_grange  then 'grange'
             else                   'religious' end
      when has_personal_struct then 'surname_review'   -- a family; DO NOT MINT
      else                          'review'           -- ambiguous; DO NOT MINT
    end as institution_kind
  from base b
)
select
  county_fips, parcel_apn, owner_name, property_class, vintage_flag, acres,
  institution_kind,
  -- MINTABLE: a proven institution only. surname_review / review / govt / cemetery /
  -- school / medical are all FALSE — they never auto-mint. Keep in lockstep with the
  -- has_org_token gate in scripts/mint_discovery (mine_exempt).
  (institution_kind in ('religious', 'fraternal_lodge', 'veterans', 'grange')) as mintable
from classified;

comment on view landos.v_mine_exempt_institutional is
  '🔴 REGEX-DERIVED, NOT exempt-roll-derived. CONVERSION lens: church/lodge/VFW/grange/govt '
  'candidates from landos.parcel_owner. `mintable`=true ONLY for a name carrying a '
  'denomination/fraternal/civic QUALIFIER token (structural proof of an institution). A '
  'surname trigger + personal structure ("Church Sandra", "Temple Patrick & Roxann") -> '
  'institution_kind=surname_review, mintable=false; genuinely ambiguous -> review, '
  'mintable=false. property_class does NOT discriminate here (church & the Church family '
  'both 529/530). Tuned on LaPorte 18091; there is NO authoritative exempt source behind '
  'mintable. DO NOT bulk-mint this view on a new county (e.g. Berrien 86k) without a real '
  'exempt roll to join OR the mandatory spot-check gate — it will otherwise mint families. '
  'See migration 033 + scripts/mint_discovery (mine_exempt, same has_org_token gate).';
