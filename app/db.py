"""
db.py — one shared connection pool to the Supabase Postgres database.

Why a pool: opening a brand-new database connection on every API request is
slow and wasteful. A pool keeps a small set of connections open and hands
one out per request, then takes it back. We open it once when the service
boots and reuse it for the life of the process.

How transactions work here: every time you write `with pool.connection() as
conn:`, psycopg starts a transaction. If the block finishes cleanly, it
COMMITS automatically. If an exception is raised inside, it ROLLS BACK.
That means each endpoint's multiple INSERTs either all land together or none
of them do — no half-written records.
"""
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row

from .config import DATABASE_URL

# row_factory=dict_row makes query results come back as Python dictionaries
# (e.g. row["score"]) instead of positional tuples (row[0]) — far easier to read.
pool = ConnectionPool(
    conninfo=DATABASE_URL,
    min_size=1,
    max_size=10,
    open=True,
    kwargs={"row_factory": dict_row},
)
