#!/bin/zsh
# Court for migrations/042_efxo_landos_constraints_parity.sql.
#   court_042.sh court   -> runs the whole file inside BEGIN ... ROLLBACK on the target (no persistent change)
#   court_042.sh apply   -> runs the file in ONE transaction (psql -1) and prints the post census
# Target = DEEDSONAR_DATABASE_URL from the engine .env (efxo only; lvdi refused; DSN never printed).
set -euo pipefail
MODE=${1:?court|apply}
HERE=${0:A:h}
MIG=$HERE/../migrations/042_efxo_landos_constraints_parity.sql
ENV=$HOME/neuruh-repos/NeuruhAI/landos-engine/.env
DSN=$(sed -n 's/^DEEDSONAR_DATABASE_URL=//p' $ENV | tr -d '"'"'"'')
[[ $DSN == *efxowqudmsfqjmbeidjc* && $DSN != *lvdimujkfheeyqchjzra* ]] || { print -u2 "REFUSED: target is not efxo"; exit 2; }
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
EV=${COURT_EVIDENCE_DIR:-$HOME/neuruh-workspace/T6_SUPPORTING_SYSTEM_20260913/landos_parity}; mkdir -p $EV
print "MIG sha256 $(shasum -a 256 $MIG | cut -c1-64)  mode=$MODE  stamp=$STAMP"

CENSUS="select c.relname,
  count(k.oid) filter (where k.contype='p') pk, count(k.oid) filter (where k.contype='u') uq,
  count(k.oid) filter (where k.contype='f') fk, count(k.oid) filter (where k.contype='c') ck,
  (select count(*) from pg_index i where i.indrelid=c.oid) idx
 from pg_class c join pg_namespace n on n.oid=c.relnamespace left join pg_constraint k on k.conrelid=c.oid
 where n.nspname='landos' and c.relkind='r' group by c.oid, c.relname order by 1"
ROWS="select 'audit_log', count(*) from landos.audit_log union all select 'comps', count(*) from landos.comps
 union all select 'identity_nodes', count(*) from landos.identity_nodes union all select 'scores', count(*) from landos.scores order by 1"
# the exact statement app/main.py:511 runs on POST /evaluate (probe row is rolled back in court mode; in apply mode it is not executed)
PROBE="insert into landos.identity_nodes (tenant_id, parcel_apn, owner_hash, county, state)
 values ('1840ac90-54bd-4b3d-8baf-227c8d84ab5a', 'T6-COURT-PROBE', 'probe', 'court', 'XX')
 on conflict (tenant_id, parcel_apn, owner_hash) do update set updated_at = now() returning id"

if [[ $MODE == court ]]; then
  {
    print -r -- "\\set ON_ERROR_STOP on"
    print -r -- "begin;"
    print -r -- "\\echo -- PRE census"; print "$CENSUS;"
    print -r -- "\\echo -- PRE rows"; print "$ROWS;"
    print -r -- "\\i $MIG"
    print -r -- "\\echo -- POST census (must equal the legacy spine census)"; print "$CENSUS;"
    print -r -- "\\echo -- POST rows"; print "$ROWS;"
    print -r -- "\\echo -- /evaluate upsert probe (rolled back)"; print "$PROBE;"
    print -r -- "\\echo -- probe again must hit ON CONFLICT (same id)"; print "$PROBE;"
    print -r -- "rollback;"
  } > $EV/court_$STAMP.psql
  psql "$DSN" -X -A -f $EV/court_$STAMP.psql | tee $EV/court_$STAMP.out
  print "COURT=DONE (rolled back) evidence $EV/court_$STAMP.out"
else
  psql "$DSN" -X -A -v ON_ERROR_STOP=1 -c "$CENSUS" -c "$ROWS" | tee $EV/apply_pre_$STAMP.out
  psql "$DSN" -X -A -v ON_ERROR_STOP=1 -1 -f $MIG | tee $EV/apply_$STAMP.out
  psql "$DSN" -X -A -v ON_ERROR_STOP=1 -c "$CENSUS" -c "$ROWS" | tee $EV/apply_post_$STAMP.out
  print "APPLY=DONE evidence $EV/apply_*_$STAMP.out"
fi
