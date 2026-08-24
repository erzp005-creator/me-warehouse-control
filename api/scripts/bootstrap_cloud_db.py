"""Initialise a managed PostgreSQL database for cloud deployments.

The command is designed for a pre-deploy container. It is safe to run on
every API deployment:

* an empty database receives the canonical schema and a minimal warehouse;
* an older Sentry database missing Work Control receives migration 082;
* an already-initialised database is left unchanged.

Usage:
    python scripts/bootstrap_cloud_db.py
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time

import bcrypt
import psycopg2


APP_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_FILE = APP_ROOT / "db" / "schema.sql"
WORK_CONTROL_MIGRATION = APP_ROOT / "db" / "migrations" / "082_work_control.sql"


def _connect(database_url: str, attempts: int = 15):
    """Connect with a short retry window while the managed DB starts."""
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return psycopg2.connect(database_url)
        except psycopg2.OperationalError as exc:
            last_error = exc
            if attempt == attempts:
                break
            time.sleep(2)
    raise RuntimeError("Could not connect to PostgreSQL after retrying") from last_error


def _table_exists(connection, table_name: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT to_regclass(%s) IS NOT NULL",
            (f"public.{table_name}",),
        )
        return bool(cursor.fetchone()[0])


def _run_sql_file(database_url: str, sql_file: Path) -> None:
    if not sql_file.is_file():
        raise RuntimeError(f"Required SQL file is missing: {sql_file}")
    subprocess.run(
        [
            "psql",
            "--dbname",
            database_url,
            "--no-psqlrc",
            "--set",
            "ON_ERROR_STOP=1",
            "--file",
            str(sql_file),
        ],
        check=True,
    )


def _ensure_minimal_setup(connection, admin_password: str) -> bool:
    """Create only missing pilot records and return whether anything changed."""
    changed = False
    with connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT warehouse_id FROM warehouses ORDER BY warehouse_id LIMIT 1"
            )
            warehouse_row = cursor.fetchone()
            receiving_bin_id = None

            if warehouse_row:
                warehouse_id = warehouse_row[0]
                cursor.execute(
                    """
                    SELECT bin_id
                      FROM bins
                     WHERE warehouse_id = %s
                       AND (bin_code = 'RECV-01' OR bin_type = 'Staging')
                     ORDER BY CASE WHEN bin_code = 'RECV-01' THEN 0 ELSE 1 END,
                              bin_id
                     LIMIT 1
                    """,
                    (warehouse_id,),
                )
                receiving_row = cursor.fetchone()
                receiving_bin_id = receiving_row[0] if receiving_row else None
            else:
                cursor.execute(
                    """
                    INSERT INTO warehouses
                        (warehouse_code, warehouse_name, address)
                    VALUES ('WH-01', 'ME Group Warehouse', '')
                    RETURNING warehouse_id
                    """
                )
                warehouse_id = cursor.fetchone()[0]
                changed = True

                zone_ids = {}
                for code, name, zone_type in (
                    ("RCV", "Receiving", "RECEIVING"),
                    ("PICK", "Picking", "PICKING"),
                    ("STAGE", "Staging", "STAGING"),
                ):
                    cursor.execute(
                        """
                        INSERT INTO zones
                            (warehouse_id, zone_code, zone_name, zone_type)
                        VALUES (%s, %s, %s, %s)
                        RETURNING zone_id
                        """,
                        (warehouse_id, code, name, zone_type),
                    )
                    zone_ids[code] = cursor.fetchone()[0]

                for zone_code, bin_code, bin_type, sequence, description in (
                    ("RCV", "RECV-01", "Staging", 0, "Default receiving bin"),
                    ("PICK", "PICK-01", "Pickable", 100, "Default pick bin"),
                    ("STAGE", "BULK-01", "Pickable", 0, "Default bulk bin"),
                ):
                    cursor.execute(
                        """
                        INSERT INTO bins
                            (zone_id, warehouse_id, bin_code, bin_barcode,
                             bin_type, pick_sequence, putaway_sequence,
                             description, external_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                                gen_random_uuid())
                        RETURNING bin_id
                        """,
                        (
                            zone_ids[zone_code],
                            warehouse_id,
                            bin_code,
                            bin_code,
                            bin_type,
                            sequence,
                            sequence,
                            description,
                        ),
                    )
                    bin_id = cursor.fetchone()[0]
                    if bin_code == "RECV-01":
                        receiving_bin_id = bin_id

            cursor.execute("SELECT 1 FROM users WHERE role = 'ADMIN' LIMIT 1")
            if cursor.fetchone() is None:
                if len(admin_password) < 12:
                    raise RuntimeError(
                        "ADMIN_PASSWORD must contain at least 12 characters "
                        "when no admin account exists"
                    )
                password_hash = bcrypt.hashpw(
                    admin_password.encode("utf-8"), bcrypt.gensalt()
                ).decode("utf-8")

                cursor.execute(
                    """
                    INSERT INTO users
                        (username, password_hash, full_name, role, warehouse_id,
                         allowed_functions, must_change_password, external_id)
                    VALUES ('admin', %s, 'ME Group Admin', 'ADMIN', %s, '{}',
                            FALSE, gen_random_uuid())
                    """,
                    (password_hash, warehouse_id),
                )
                changed = True

            defaults = [
                ("session_timeout_hours", "8"),
                ("require_packing_before_shipping", "true"),
                ("allow_over_receiving", "true"),
            ]
            if receiving_bin_id is not None:
                defaults.append(("default_receiving_bin", str(receiving_bin_id)))

            for key, value in defaults:
                cursor.execute(
                    """
                    INSERT INTO app_settings (key, value)
                    VALUES (%s, %s)
                    ON CONFLICT (key) DO NOTHING
                    """,
                    (key, value),
                )
    return changed


def main() -> int:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")

    connection = _connect(database_url)
    try:
        has_core_schema = _table_exists(connection, "warehouses")
        has_work_control = _table_exists(connection, "work_tasks")
    finally:
        connection.close()

    if not has_core_schema:
        admin_password = os.environ.get("ADMIN_PASSWORD", "")
        if len(admin_password) < 12:
            raise RuntimeError(
                "ADMIN_PASSWORD must contain at least 12 characters for a fresh database"
            )
        print("Empty database detected; loading the canonical schema.", flush=True)
        _run_sql_file(database_url, SCHEMA_FILE)
    elif not has_work_control:
        print("Existing database detected; applying migration 082.", flush=True)
        _run_sql_file(database_url, WORK_CONTROL_MIGRATION)
    else:
        print("Database schema is already current; no changes required.", flush=True)

    connection = _connect(database_url)
    try:
        setup_changed = _ensure_minimal_setup(
            connection, os.environ.get("ADMIN_PASSWORD", "")
        )
        if setup_changed:
            print("Minimal warehouse and admin account created.", flush=True)
        if not _table_exists(connection, "warehouses") or not _table_exists(
            connection, "work_tasks"
        ):
            raise RuntimeError("Database bootstrap verification failed")
    finally:
        connection.close()

    print("Cloud database bootstrap verified.", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # pre-deploy command must fail loudly
        print(f"Cloud database bootstrap failed: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
