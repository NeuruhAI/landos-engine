#!/usr/bin/env python3
"""Fixed-purpose, read-only migration-038 inspector.  It accepts no SQL or paths."""
from __future__ import annotations
import hashlib, json, sys
from datetime import datetime, timezone
from pathlib import Path

OP='inspect_field_observation_evidence'; SCHEMA='landos'; TABLE='field_observation_evidence'
MIGRATION=Path(__file__).resolve().parents[1]/'migrations/038_field_observation_evidence.sql'
ERRORS=('DB_CONFIGURATION_UNAVAILABLE','DB_CONNECTION_FAILED','DB_READONLY_NOT_ENFORCED','SCHEMA_NOT_FOUND','TABLE_NOT_FOUND','MIGRATION_FILE_NOT_FOUND','MIGRATION_HASH_MISMATCH','QUERY_FAILED','RECEIPT_INVALID')
def fail(code):
    print(json.dumps({'ok':False,'operation':OP,'schema':SCHEMA,'table':TABLE,'schema_exists':False,'table_exists':False,'columns':[],'row_count':None,'transaction_read_only':False,'migration_file':str(MIGRATION),'migration_sha256':None,'database_identity_hash':None,'checked_at':datetime.now(timezone.utc).isoformat(),'error_code':code},sort_keys=True)); return 1
def main():
    if not MIGRATION.is_file(): return fail('MIGRATION_FILE_NOT_FOUND')
    migration_sha=hashlib.sha256(MIGRATION.read_bytes()).hexdigest()
    try:
        DATABASE_URL=__import__('os').environ.get('DATABASE_URL')
        if not DATABASE_URL: return fail('DB_CONFIGURATION_UNAVAILABLE')
        import psycopg
    except Exception:return fail('DB_CONFIGURATION_UNAVAILABLE')
    try:
        with psycopg.connect(DATABASE_URL, autocommit=False) as conn:
            with conn.cursor() as cur:
                cur.execute('SET TRANSACTION READ ONLY')
                cur.execute('SHOW transaction_read_only'); ro=str(cur.fetchone()[0]).lower()=='on'
                if not ro:return fail('DB_READONLY_NOT_ENFORCED')
                cur.execute("SELECT current_database(), current_user")
                ident=hashlib.sha256('|'.join(map(str,cur.fetchone())).encode()).hexdigest()
                cur.execute("SELECT EXISTS (SELECT 1 FROM information_schema.schemata WHERE schema_name='landos')")
                schema_exists=bool(cur.fetchone()[0])
                if not schema_exists:return fail('SCHEMA_NOT_FOUND')
                cur.execute("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='landos' AND table_name='field_observation_evidence')")
                table_exists=bool(cur.fetchone()[0])
                if not table_exists:return fail('TABLE_NOT_FOUND')
                cur.execute("SELECT column_name,data_type FROM information_schema.columns WHERE table_schema='landos' AND table_name='field_observation_evidence' ORDER BY ordinal_position")
                columns=[{'name':r[0],'type':r[1]} for r in cur.fetchall()]
                cur.execute('SELECT count(*) FROM landos.field_observation_evidence')
                row_count=int(cur.fetchone()[0])
            conn.rollback()
    except Exception:return fail('DB_CONNECTION_FAILED')
    out={'ok':True,'operation':OP,'schema':SCHEMA,'table':TABLE,'schema_exists':schema_exists,'table_exists':table_exists,'columns':columns,'row_count':row_count,'transaction_read_only':True,'migration_file':str(MIGRATION),'migration_sha256':migration_sha,'database_identity_hash':ident,'checked_at':datetime.now(timezone.utc).isoformat(),'error_code':None}
    required=('ok','operation','schema','table','schema_exists','table_exists','columns','row_count','transaction_read_only','migration_file','migration_sha256','database_identity_hash','checked_at','error_code')
    if any(k not in out for k in required) or not out['transaction_read_only']: return fail('RECEIPT_INVALID')
    print(json.dumps(out,sort_keys=True)); return 0
if __name__=='__main__': sys.exit(main())
