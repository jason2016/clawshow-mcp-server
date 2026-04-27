"""
Backfill referral_code for legacy dragons-elysees customers.

Idempotent: safe to run multiple times.
Only updates customers whose referral_code is NULL or empty.

Usage on stand9:
    cd /opt/clawshow-mcp-server
    python3 scripts/backfill_referral_codes.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.dragons_elysees_db import get_conn, generate_unique_referral_code


def backfill():
    """Generate referral_code for all customers missing one."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, email FROM customers WHERE referral_code IS NULL OR referral_code = ''"
        ).fetchall()

        if not rows:
            print("No customers need backfilling. All have referral_code.")
            return

        print(f"Found {len(rows)} customers without referral_code:")
        for row in rows:
            print(f"  - id={row['id']} email={row['email']}")

        updated = 0
        for row in rows:
            code = generate_unique_referral_code()
            conn.execute(
                "UPDATE customers SET referral_code = ? WHERE id = ? AND (referral_code IS NULL OR referral_code = '')",
                (code, row["id"]),
            )
            print(f"  id={row['id']} email={row['email']} -> code={code}")
            updated += 1

        print(f"\nBackfilled {updated} customers.")


if __name__ == "__main__":
    backfill()
