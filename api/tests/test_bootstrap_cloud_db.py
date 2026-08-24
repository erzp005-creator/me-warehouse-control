import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bootstrap_cloud_db.py"


def load_script():
    spec = importlib.util.spec_from_file_location("bootstrap_cloud_db_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cloud_bootstrap_bakes_required_sql_paths():
    module = load_script()
    assert module.SCHEMA_FILE.name == "schema.sql"
    assert module.WORK_CONTROL_MIGRATION.name == "082_work_control.sql"


def test_cloud_bootstrap_requires_database_url(monkeypatch):
    module = load_script()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL is required"):
        module.main()


def test_fresh_database_requires_strong_admin_password(monkeypatch):
    module = load_script()
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("ADMIN_PASSWORD", "short")

    class FakeConnection:
        def close(self):
            return None

    monkeypatch.setattr(module, "_connect", lambda _url: FakeConnection())
    existence = iter((False, False))
    monkeypatch.setattr(module, "_table_exists", lambda _conn, _table: next(existence))

    with pytest.raises(RuntimeError, match="at least 12 characters"):
        module.main()
