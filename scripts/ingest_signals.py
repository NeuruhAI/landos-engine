"""
ingest_signals.py — run signal normalizers (PUBLIC records only) into landos.signals.

    python -m scripts.ingest_signals             # all configured Berrien Tier-1 catalogs
    python -m scripts.ingest_signals 2906        # a single tax-sale catalog id

Tier-1 source = tax-sale.info (Michigan tax-foreclosure auctions). The 2026 MAIN
Berrien county catalog was not yet posted at build time, so we ingest the LIVE
2026 Berrien DNR catalog (2906) + the 2022 main Berrien catalog (2049, real BH
parcels), each tagged with its TRUE signal_date + provenance. When the 2026 main
catalog posts (watch https://www.tax-sale.info/ for Berrien -> "Ready to Browse"),
add its id below — no code change, just a row.

Every fetch is audit-logged to landos.signal_ingest_log (source, timestamp, rows).
"""
import sys

import psycopg

from app.config import DATABASE_URL
from app.normalizers.tax_sale_info import TaxSaleInfoNormalizer
from app.normalizers.egle_brownfield import EgleBrownfieldNormalizer
from app.normalizers.egle_part201 import EglePart201Normalizer

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"

# (catalog_id, label, signal_year)
BERRIEN_TAX_CATALOGS = [
    (2906, "Berrien DNR / state land — Aug 6 2026 sale", 2026),
    (2049, "Berrien main county tax-foreclosure (2022 cycle)", 2022),
    # (XXXX, "Berrien main county — Aug 6 2026 sale", 2026),  # add when posted
]


def main(argv) -> None:
    catalogs = BERRIEN_TAX_CATALOGS
    if argv:
        cid = int(argv[0])
        catalogs = [c for c in BERRIEN_TAX_CATALOGS if c[0] == cid] or [(cid, f"catalog {cid}", 2026)]
    tax_total = 0
    other_total = 0
    with psycopg.connect(DATABASE_URL) as conn:
        for cid, label, year in catalogs:
            norm = TaxSaleInfoNormalizer(TENANT, cid, label, year)
            try:
                n = norm.run(conn)
                print(f"  {norm.source_id}: {n} signals  [{label}]")
                tax_total += n
            except Exception as e:  # noqa: BLE001
                print(f"  catalog {cid}: ERROR {e.__class__.__name__}: {e}")
        # Tier-2 sources run only on a full ingest (no specific catalog requested).
        if not argv:
            for norm in (EgleBrownfieldNormalizer(TENANT), EglePart201Normalizer(TENANT)):
                try:
                    n = norm.run(conn)
                    print(f"  {norm.source_id}: {n} signals  [{norm.name}]")
                    other_total += n
                except Exception as e:  # noqa: BLE001
                    print(f"  {norm.source_id}: ERROR {e.__class__.__name__}: {e}")
    print(f"Done. {tax_total} tax-delinquent + {other_total} other signal(s) ingested.")


if __name__ == "__main__":
    main(sys.argv[1:])
