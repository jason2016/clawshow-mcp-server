"""Initialize ClawShow database. Run this on first deploy or to reset tables."""

from db import init_tables, DB_PATH

if __name__ == "__main__":
    init_tables()
    print(f"Database initialized at: {DB_PATH}")
