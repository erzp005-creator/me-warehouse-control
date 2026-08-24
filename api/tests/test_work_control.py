"""End-to-end coverage for the ME Warehouse Control extension."""

import io
from datetime import date

import bcrypt

from db_test_context import get_raw_connection


def _create_worker(username, functions):
    password = "Warehouse9"
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO users
            (username, password_hash, full_name, role, warehouse_id,
             warehouse_ids, allowed_functions, external_id)
        VALUES (%s, %s, %s, 'USER', 1, ARRAY[1]::int[], %s, gen_random_uuid())
        """,
        (username, password_hash, username.title(), functions),
    )
    cur.close()
    return password


def _login(client, username, password):
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.get_json()['token']}"}


def _batch_payload(order_count=2):
    return {
        "warehouse_id": 1,
        "source_system": "sitegiant",
        "pack_note_ref": "2950",
        "platform": "TikTok",
        "priority": 60,
        "orders": [
            {
                "order_number": f"TTS-{index:05d}",
                "courier_barcode": f"MY-CARRIER-{index:05d}",
                "sku_count": 2,
                "unit_count": 3,
            }
            for index in range(1, order_count + 1)
        ],
        "task_types": ["PICKING", "PACKING"],
    }


def test_pack_note_batch_limits_and_any_barcode_lookup(client, auth_headers):
    too_large = client.post(
        "/api/work-control/batches",
        headers=auth_headers,
        json=_batch_payload(51),
    )
    assert too_large.status_code == 400

    created = client.post(
        "/api/work-control/batches",
        headers=auth_headers,
        json=_batch_payload(),
    )
    assert created.status_code == 201
    body = created.get_json()
    assert body["pack_note_ref"] == "2950"
    assert len(body["orders"]) == 2
    assert {task["task_type"] for task in body["tasks"]} == {"PICKING", "PACKING"}

    lookup = client.get(
        "/api/work-control/scan/MY-CARRIER-00002",
        headers=auth_headers,
    )
    assert lookup.status_code == 200
    assert lookup.get_json()["batch"]["batch_id"] == body["batch_id"]


def test_pick_and_pack_are_concurrent_scan_gated_and_attributed(client, auth_headers):
    picker_password = _create_worker("wc_picker", ["work", "pick"])
    packer_password = _create_worker("wc_packer", ["work", "pack"])
    picker_headers = _login(client, "wc_picker", picker_password)
    packer_headers = _login(client, "wc_packer", packer_password)

    created = client.post(
        "/api/work-control/batches",
        headers=auth_headers,
        json=_batch_payload(),
    ).get_json()

    picker_task = client.post(
        "/api/work-control/tasks/claim-next",
        headers=picker_headers,
        json={"warehouse_id": 1, "task_types": ["PICKING"]},
    ).get_json()["task"]
    packer_task = client.post(
        "/api/work-control/tasks/claim-next",
        headers=packer_headers,
        json={"warehouse_id": 1, "task_types": ["PACKING"]},
    ).get_json()["task"]
    assert picker_task["batch_id"] == packer_task["batch_id"] == created["batch_id"]
    assert picker_task["claimed_by"] == "wc_picker"
    assert packer_task["claimed_by"] == "wc_packer"

    bypass = client.post(
        f"/api/work-control/tasks/{picker_task['task_id']}/transition",
        headers=picker_headers,
        json={"action": "START"},
    )
    assert bypass.status_code == 409
    assert "Scan one courier barcode" in bypass.get_json()["error"]

    wrong_scan = client.post(
        f"/api/work-control/tasks/{picker_task['task_id']}/verify-scan",
        headers=picker_headers,
        json={"barcode": "NOT-IN-THIS-PACK-NOTE"},
    )
    assert wrong_scan.status_code == 409

    for task, headers in ((picker_task, picker_headers), (packer_task, packer_headers)):
        verified = client.post(
            f"/api/work-control/tasks/{task['task_id']}/verify-scan",
            headers=headers,
            json={"barcode": "MY-CARRIER-00001"},
        )
        assert verified.status_code == 200
        started = client.post(
            f"/api/work-control/tasks/{task['task_id']}/transition",
            headers=headers,
            json={"action": "START"},
        )
        assert started.status_code == 200
        assert started.get_json()["task"]["status"] == "IN_PROGRESS"

    reported = client.post(
        "/api/work-control/errors",
        headers=packer_headers,
        json={
            "warehouse_id": 1,
            "task_id": packer_task["task_id"],
            "batch_id": created["batch_id"],
            "error_type": "WRONG_QUANTITY",
            "severity": "MEDIUM",
            "description": "Cross-check found one unit missing",
        },
    )
    assert reported.status_code == 201
    case = reported.get_json()
    assert case["picker_user_id"] == "wc_picker"
    assert case["packer_user_id"] == "wc_packer"

    reviewed = client.post(
        f"/api/work-control/errors/{case['error_id']}/review",
        headers=auth_headers,
        json={
            "status": "CONFIRMED",
            "responsibility": "PICKER",
            "resolution_notes": "Picker recount confirmed short quantity",
        },
    )
    assert reviewed.status_code == 200

    today = date.today().isoformat()
    report = client.get(
        f"/api/work-control/reports/efficiency?warehouse_id=1&start={today}&end={today}",
        headers=auth_headers,
    )
    assert report.status_code == 200
    assert report.get_json()["scoring_applied"] is False
    assert {
        (row["employee"], row["stage"], row["confirmed_errors"])
        for row in report.get_json()["confirmed_errors"]
    } == {("wc_picker", "PICKING", 1)}


def test_worker_cannot_claim_an_ungranted_task_type(client):
    password = _create_worker("wc_pack_only", ["work", "pack"])
    headers = _login(client, "wc_pack_only", password)
    denied = client.post(
        "/api/work-control/tasks/claim-next",
        headers=headers,
        json={"warehouse_id": 1, "task_types": ["PICKING"]},
    )
    assert denied.status_code == 403
    assert "not granted" in denied.get_json()["error"]


def test_receiving_requires_photo_then_continues_and_rejection_queues_recount(
    client, auth_headers, monkeypatch, tmp_path,
):
    monkeypatch.setenv("EVIDENCE_STORAGE_DIR", str(tmp_path / "evidence"))
    receiver_password = _create_worker("wc_receiver", ["work", "receive"])
    receiver_headers = _login(client, "wc_receiver", receiver_password)

    created = client.post(
        "/api/work-control/tasks",
        headers=auth_headers,
        json={
            "warehouse_id": 1,
            "task_type": "RECEIVING",
            "priority": 70,
            "assigned_to": "wc_receiver",
            "source_ref": "DELIVERY-88",
            "sku_count": 1,
            "unit_count": 12,
        },
    )
    assert created.status_code == 201
    task_id = created.get_json()["task"]["task_id"]

    claimed = client.post(
        "/api/work-control/tasks/claim-next",
        headers=receiver_headers,
        json={"warehouse_id": 1, "task_types": ["RECEIVING"]},
    ).get_json()["task"]
    assert claimed["task_id"] == task_id
    assert client.post(
        f"/api/work-control/tasks/{task_id}/transition",
        headers=receiver_headers,
        json={"action": "START"},
    ).status_code == 200

    draft_response = client.post(
        "/api/work-control/receiving-drafts",
        headers=receiver_headers,
        json={
            "warehouse_id": 1,
            "task_id": task_id,
            "source_system": "sitegiant",
            "po_number": "DELIVERY-88",
            "lines": [{
                "sku": "TST-001",
                "expected_quantity": 10,
                "received_quantity": 12,
                "good_quantity": 11,
                "damaged_quantity": 1,
            }],
        },
    )
    assert draft_response.status_code == 201
    draft = draft_response.get_json()["receiving"]
    assert draft["lines"][0]["over_quantity"] == 2

    no_photo = client.post(
        f"/api/work-control/receiving-drafts/{draft['receiving_id']}/submit",
        headers=receiver_headers,
        json={"claim_next": True, "next_task_types": ["RECEIVING"]},
    )
    assert no_photo.status_code == 409

    upload = client.post(
        "/api/work-control/evidence",
        headers=receiver_headers,
        data={
            "receiving_id": str(draft["receiving_id"]),
            "photo": (io.BytesIO(b"\xff\xd8\xff" + b"evidence" * 20), "arrival.jpg"),
        },
        content_type="multipart/form-data",
    )
    assert upload.status_code == 201
    evidence_id = upload.get_json()["evidence_id"]
    downloaded = client.get(
        f"/api/work-control/evidence/{evidence_id}",
        headers=receiver_headers,
    )
    assert downloaded.status_code == 200
    assert downloaded.data == b"\xff\xd8\xff" + b"evidence" * 20

    submitted = client.post(
        f"/api/work-control/receiving-drafts/{draft['receiving_id']}/submit",
        headers=receiver_headers,
        json={"claim_next": True, "next_task_types": ["RECEIVING"]},
    )
    assert submitted.status_code == 200
    assert submitted.get_json()["receiving"]["status"] == "SUBMITTED"
    assert submitted.get_json()["next_task"] is None

    rejected = client.post(
        f"/api/work-control/receiving-drafts/{draft['receiving_id']}/review",
        headers=auth_headers,
        json={"status": "REJECTED", "review_notes": "Label is unclear; recount"},
    )
    assert rejected.status_code == 200
    recount_task_id = rejected.get_json()["recount_task_id"]
    assert recount_task_id is not None

    recount = client.post(
        "/api/work-control/tasks/claim-next",
        headers=receiver_headers,
        json={"warehouse_id": 1, "task_types": ["RECEIVING"]},
    )
    assert recount.status_code == 200
    assert recount.get_json()["task"]["task_id"] == recount_task_id
    assert recount.get_json()["task"]["assigned_to"] == "wc_receiver"
