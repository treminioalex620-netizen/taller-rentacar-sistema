"""Copy the existing SQLite data to an empty PostgreSQL database.

Set DATABASE_URL before running this script. It intentionally stops when the
target has business data, so it cannot overwrite a live PostgreSQL database.
"""
import os
import sqlite3
import sys
from pathlib import Path

import psycopg

BASE_DIR = Path(__file__).resolve().parent
SOURCE_DB = Path(os.environ.get("SQLITE_SOURCE", BASE_DIR / "taller_rentacar.db"))
TARGET_URL = os.environ.get("DATABASE_URL")
TABLES = ("users", "employees", "incomes", "invoices", "expenses", "day_closures", "payroll_payments", "audit_logs")


def main():
    if not TARGET_URL or not TARGET_URL.startswith(("postgres://", "postgresql://")):
        sys.exit("Defina DATABASE_URL con la conexión PostgreSQL de destino.")
    if not SOURCE_DB.exists():
        sys.exit(f"No se encontró la base SQLite: {SOURCE_DB}")

    # Creates the PostgreSQL schema and the initial application account.
    from app import init_db
    init_db()

    source = sqlite3.connect(SOURCE_DB)
    source.row_factory = sqlite3.Row
    target = psycopg.connect(TARGET_URL)
    try:
        for table in TABLES[1:]:
            if target.execute(f"SELECT EXISTS (SELECT 1 FROM {table} LIMIT 1)").fetchone()[0]:
                sys.exit("El destino ya tiene datos. La migración se detuvo para protegerlos.")

        source_tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in TABLES:
            if table not in source_tables:
                continue
            columns = [row[1] for row in source.execute(f"PRAGMA table_info({table})")]
            rows = source.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()
            if not rows:
                continue
            fields = ", ".join(columns)
            placeholders = ", ".join(["%s"] * len(columns))
            conflict = "ON CONFLICT (id) DO NOTHING"
            if table == "users":
                conflict = "ON CONFLICT (id) DO UPDATE SET username=EXCLUDED.username, password_hash=EXCLUDED.password_hash, role=EXCLUDED.role, active=EXCLUDED.active"
            with target.cursor() as cursor:
                cursor.executemany(f"INSERT INTO {table} ({fields}) VALUES ({placeholders}) {conflict}", [tuple(row[column] for column in columns) for row in rows])
            target.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), COALESCE((SELECT MAX(id) FROM {table}), 1), true)")
        target.commit()
        print("Migración finalizada correctamente.")
    finally:
        source.close()
        target.close()


if __name__ == "__main__":
    main()
