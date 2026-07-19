import json, subprocess, sys
from pathlib import Path

SCRIPT=Path(__file__).parents[1]/'scripts/inspect_field_observation_evidence.py'
SOURCE=SCRIPT.read_text()

def test_fixed_scope_and_no_mutation_queries():
    assert "SCHEMA='landos'" in SOURCE and "TABLE='field_observation_evidence'" in SOURCE
    assert 'execute(sql' not in SOURCE and 'input(' not in SOURCE
    for word in ('INSERT','UPDATE','DELETE','TRUNCATE','ALTER','CREATE','DROP','GRANT','REVOKE'):
        assert word not in SOURCE.upper().replace('TRANSACTION READ ONLY','')

def test_missing_configuration_fails_closed():
    p=subprocess.run([sys.executable,str(SCRIPT)],cwd=SCRIPT.parents[1],env={'PATH':'/usr/bin:/bin'},capture_output=True,text=True)
    assert p.returncode != 0
    out=json.loads(p.stdout); assert out['ok'] is False and out['error_code'] in {'DB_CONFIGURATION_UNAVAILABLE','DB_CONNECTION_FAILED'}

def test_receipt_shape_is_declared():
    for key in ('schema_exists','table_exists','columns','row_count','transaction_read_only','migration_sha256','database_identity_hash'):
        assert key in SOURCE
