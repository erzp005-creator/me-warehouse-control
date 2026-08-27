"""Personal work reports expose facts for the signed-in employee only."""

from secrets import token_urlsafe

import bcrypt

from db_test_context import get_raw_connection


def _create_worker(username):
    password = token_urlsafe(18)
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO users
            (username, password_hash, full_name, role, warehouse_id,
             warehouse_ids, allowed_functions, external_id)
        VALUES (%s, %s, %s, 'USER', 1, ARRAY[1]::int[],
                ARRAY['work', 'pick']::text[], gen_random_uuid())
        """,
        (username, password_hash, username.title()),
    )
    cur.close()
    return password


def _login(client, username, password):
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.get_json()['token']}"}


def test_personal_report_is_scoped_and_unscored(client):
    password = _create_worker("wc_personal")
    _create_worker("wc_personal_other")
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO work_tasks
            (warehouse_id, task_type, status, assigned_to, claimed_by,
             source_ref, order_count, sku_count, unit_count, completed_at,
             active_seconds, paused_seconds, created_by)
        VALUES
            (1, 'PICKING', 'COMPLETED', 'wc_personal', 'wc_personal',
             'PERSONAL-50', 50, 4, 65, NOW(), 120, 30, 'admin'),
            (1, 'PICKING', 'COMPLETED', 'wc_personal_other', 'wc_personal_other',
             'OTHER-50', 999, 99, 999, NOW(), 9999, 0, 'admin')
        RETURNING task_id, claimed_by
        """
    )
    task_rows = cur.fetchall()
    own_task_id = next(row[0] for row in task_rows if row[1] == "wc_personal")
    cur.execute(
        """
        INSERT INTO work_errors
            (warehouse_id, task_id, error_type, status, responsibility,
             discovered_stage, reported_by, picker_user_id, description,
             reviewed_by, reviewed_at)
        VALUES
            (1, %s, 'WRONG_QUANTITY', 'CONFIRMED', 'PICKER',
             'PACKING', 'checker', 'wc_personal', 'Confirmed short count',
             'admin', NOW()),
            (1, %s, 'DAMAGED_ITEM', 'PENDING', 'UNCONFIRMED',
             'PICKING', 'wc_personal', 'wc_personal', 'Reported damage',
             NULL, NULL)
        """,
        (own_task_id, own_task_id),
    )
    cur.close()

    headers = _login(client, "wc_personal", password)
    response = client.get(
        "/api/work-control/reports/me?warehouse_id=1&employee=wc_personal_other",
        headers=headers,
    )
    assert response.status_code == 200
    body = response.get_json()
    today = body["periods"]["today"]
    assert body["employee"] == "wc_personal"
    assert body["scoring_applied"] is False
    assert body["ranking_applied"] is False
    assert today["summary"] == {
        "completed_tasks": 1,
        "orders_handled": 50,
        "skus_handled": 4,
        "units_handled": 65,
        "active_seconds": 120,
        "paused_seconds": 30,
        "average_active_seconds": 120,
        "reported_issues": 1,
        "pending_reported_issues": 1,
        "confirmed_mistakes": 1,
    }
    assert today["activity"][0]["task_type"] == "PICKING"
    assert today["recent"][0]["reference"] == "PERSONAL-50"

