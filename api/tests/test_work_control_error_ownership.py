"""Worker issue reports must stay tied to the employee's own task."""

from secrets import token_urlsafe

import bcrypt

from db_test_context import get_raw_connection


def _worker(username):
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
                ARRAY['work', 'pack']::text[], gen_random_uuid())
        """,
        (username, password_hash, username.title()),
    )
    cur.close()
    login = password
    return login


def _headers(client, username, password):
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.get_json()['token']}"}


def _task(client, auth_headers, assigned_to, source_ref):
    response = client.post(
        "/api/work-control/tasks",
        headers=auth_headers,
        json={
            "warehouse_id": 1,
            "task_type": "PACKING",
            "assigned_to": assigned_to,
            "source_ref": source_ref,
            "order_count": 1,
        },
    )
    assert response.status_code == 201
    return response.get_json()["task"]


def test_worker_can_report_only_from_own_task(client, auth_headers):
    reporter_password = _worker("wc_issue_reporter")
    _worker("wc_issue_other")
    reporter_headers = _headers(client, "wc_issue_reporter", reporter_password)
    own = _task(client, auth_headers, "wc_issue_reporter", "OWN-ISSUE")
    other = _task(client, auth_headers, "wc_issue_other", "OTHER-ISSUE")

    payload = {
        "warehouse_id": 1,
        "task_id": other["task_id"],
        "error_type": "WRONG_QUANTITY",
        "severity": "MEDIUM",
        "description": "A cross-check issue",
    }
    denied = client.post(
        "/api/work-control/errors",
        headers=reporter_headers,
        json=payload,
    )
    assert denied.status_code == 403

    payload["task_id"] = own["task_id"]
    created = client.post(
        "/api/work-control/errors",
        headers=reporter_headers,
        json=payload,
    )
    assert created.status_code == 201
    assert created.get_json()["responsibility"] == "UNCONFIRMED"


def test_worker_cannot_create_an_unscoped_issue(client):
    password = _worker("wc_issue_unscoped")
    headers = _headers(client, "wc_issue_unscoped", password)
    denied = client.post(
        "/api/work-control/errors",
        headers=headers,
        json={
            "warehouse_id": 1,
            "error_type": "OTHER",
            "severity": "LOW",
            "description": "No task supplied",
        },
    )
    assert denied.status_code == 403

