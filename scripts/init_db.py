"""Run migrations, create FTS indexes, bootstrap the admin and load sample metadata (idempotent)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from erpdm.db.init_db import init_database  # noqa: E402

if __name__ == "__main__":
    for notice in init_database():
        print(f"NOTICE: {notice}")
    print("Database ready.")
