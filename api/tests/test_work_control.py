"""End-to-end coverage for the ME Warehouse Control extension."""

import io
from datetime import date, datetime, timedelta, timezone

import bcrypt

from _wms_token_helpers import delete_token, insert_token
from db_test_context import get_raw_connection
from services import token_cache


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


def _batch_payload(order_count=2, *, pack_note_ref="2950", declared_order_count=None):
    payload = {
        "warehouse_id": 1,
        "source_system": "sitegiant",
        "pack_note_ref": pack_note_ref,
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
    if declared_order_count is not None:
        payload["declared_order_count"] = declared_order_count
    return payload


def _sitegiant_snapshot_payload(**overrides):
    payload = {
        "warehouse_id": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "period_start": "2026-08-19",
        "period_end": "2026-08-25",
        "period_label": "From 19 Aug 2026 to 25 Aug 2026",
        "pending_packages": 5,
        "to_process_packages": 164,
        "printed_packages": 1527,
        "pending_pickup_packages": 0,
        "dashboard_order_count": 1641,
        "source_url": "https://sitegiant.co/dashboard",
        "idempotency_key": "sitegiant-20260825T1200Z",
    }
    payload.update(overrides)
    return payload


def test_sitegiant_hourly_snapshot_is_scoped_idempotent_and_reported(
    client, auth_headers,
):
    token_id = insert_token(
        name="sitegiant-hourly",
        plaintext="sitegiant-hourly-secret",
        endpoints=["sitegiant.capture"],
        warehouse_ids=[1],
    )
    token_cache.clear()
    try:
        payload = _sitegiant_snapshot_payload(
            pending_packages=0,
            to_process_packages=0,
            printed_packages=0,
            dashboard_order_count=0,
        )
        first = client.post(
            "/api/work-control/sitegiant/workload-snapshots",
            headers={"X-WMS-Token": "sitegiant-hourly-secret"},
            json=payload,
        )
        assert first.status_code == 201
        body = first.get_json()
        assert body["duplicate"] is False
        assert body["snapshot"]["remaining_packages"] == 0
        assert body["snapshot"]["visible_total_packages"] == 0
        assert body["snapshot"]["unprocessed_percent"] == 0.0

        duplicate = client.post(
            "/api/work-control/sitegiant/workload-snapshots",
            headers={"X-WMS-Token": "sitegiant-hourly-secret"},
            json=_sitegiant_snapshot_payload(captured_at=payload["captured_at"]),
        )
        assert duplicate.status_code == 200
        duplicate_body = duplicate.get_json()
        assert duplicate_body["duplicate"] is True
        assert duplicate_body["updated"] is True
        assert duplicate_body["snapshot"]["remaining_packages"] == 169
        assert duplicate_body["snapshot"]["visible_total_packages"] == 1696

        later = client.post(
            "/api/work-control/sitegiant/workload-snapshots",
            headers={"X-WMS-Token": "sitegiant-hourly-secret"},
            json=_sitegiant_snapshot_payload(
                captured_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                to_process_packages=150,
                printed_packages=1541,
                idempotency_key="sitegiant-20260825T1300Z",
            ),
        )
        assert later.status_code == 201

        report = client.get(
            "/api/work-control/sitegiant/workload?warehouse_id=1&hours=24",
            headers=auth_headers,
        )
        assert report.status_code == 200
        report_body = report.get_json()
        assert len(report_body["snapshots"]) == 2
        assert report_body["latest"]["remaining_packages"] == 155
        assert report_body["change"] == {
            "remaining_packages": -14,
            "printed_packages": 14,
        }
        assert report_body["forecast"] == {
            "unprinted_packages": 155,
            "pack_note_capacity": 50,
            "estimated_pack_notes": 4,
            "estimated_picking_minutes": 93,
            "estimated_packing_minutes": 124,
            "estimated_total_labor_minutes": 217,
            "estimated_one_picker_one_packer_minutes": 154,
            "rates": {
                "PICKING": {
                    "minutes_per_50": 30.0,
                    "source": "baseline",
                    "sample_tasks": 0,
                    "sample_orders": 0,
                },
                "PACKING": {
                    "minutes_per_50": 40.0,
                    "source": "baseline",
                    "sample_tasks": 0,
                    "sample_orders": 0,
                },
            },
            "history_threshold": {
                "completed_tasks": 5,
                "completed_orders": 100,
                "lookback_days": 30,
            },
        }
        assert report_body["sync"]["status"] == "current"
    finally:
        token_cache.clear()
        delete_token(token_id)


def test_sitegiant_snapshot_rejects_wrong_endpoint_and_warehouse_scope(client):
    wrong_endpoint_id = insert_token(
        name="sitegiant-wrong-endpoint",
        plaintext="sitegiant-wrong-endpoint-secret",
        endpoints=["events.poll"],
        warehouse_ids=[1],
    )
    wrong_warehouse_id = insert_token(
        name="sitegiant-wrong-warehouse",
        plaintext="sitegiant-wrong-warehouse-secret",
        endpoints=["sitegiant.capture"],
        warehouse_ids=[2],
    )
    token_cache.clear()
    try:
        endpoint_denied = client.post(
            "/api/work-control/sitegiant/workload-snapshots",
            headers={"X-WMS-Token": "sitegiant-wrong-endpoint-secret"},
            json=_sitegiant_snapshot_payload(),
        )
        assert endpoint_denied.status_code == 403
        assert endpoint_denied.get_json() == {"error": "endpoint_scope_violation"}

        warehouse_denied = client.post(
            "/api/work-control/sitegiant/workload-snapshots",
            headers={"X-WMS-Token": "sitegiant-wrong-warehouse-secret"},
            json=_sitegiant_snapshot_payload(),
        )
        assert warehouse_denied.status_code == 403
        assert warehouse_denied.get_json() == {"error": "warehouse_scope_violation"}
    finally:
        token_cache.clear()
        delete_token(wrong_endpoint_id)
        delete_token(wrong_warehouse_id)


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


def test_declared_order_count_and_pack_note_or_boundary_order_scans(
    client, auth_headers,
):
    payload = _batch_payload(
        2, pack_note_ref="3440", declared_order_count=50,
    )
    payload["orders"][0]["order_number"] = "585724859994375184"
    payload["orders"][1]["order_number"] = "585726794616898650"
    payload["orders"][0]["courier_barcode"] = None
    payload["orders"][1]["courier_barcode"] = None

    response = client.post(
        "/api/work-control/batches", headers=auth_headers, json=payload,
    )
    assert response.status_code == 201
    batch = response.get_json()
    assert batch["order_count"] == 50
    assert len(batch["orders"]) == 2
    assert {task["order_count"] for task in batch["tasks"]} == {50}

    for scan in ("3440", "585724859994375184", "585726794616898650"):
        lookup = client.get(f"/api/work-control/scan/{scan}", headers=auth_headers)
        assert lookup.status_code == 200
        assert lookup.get_json()["batch"]["batch_id"] == batch["batch_id"]

    password = _create_worker("wc_pack_note_scan", ["work", "pick", "pack"])
    worker_headers = _login(client, "wc_pack_note_scan", password)
    task = client.post(
        "/api/work-control/tasks/claim-next",
        headers=worker_headers,
        json={"warehouse_id": 1, "task_types": ["PICKING", "PACKING"]},
    ).get_json()["task"]
    verified = client.post(
        f"/api/work-control/tasks/{task['task_id']}/verify-scan",
        headers=worker_headers,
        json={"barcode": "3440"},
    )
    assert verified.status_code == 200
    assert verified.get_json()["matched_order"] == {
        "batch_order_id": None,
        "order_number": None,
        "scan_kind": "PACK_NOTE",
    }
    started = client.post(
        f"/api/work-control/tasks/{task['task_id']}/transition",
        headers=worker_headers,
        json={"action": "START"},
    )
    assert started.status_code == 200


def test_four_equal_workers_get_unique_tasks_and_completion_auto_assigns_next(
    client, auth_headers,
):
    batches = []
    for pack_note in ("SIM-3440", "SIM-3441", "SIM-3442"):
        response = client.post(
            "/api/work-control/batches",
            headers=auth_headers,
            json=_batch_payload(
                2, pack_note_ref=pack_note, declared_order_count=50,
            ),
        )
        assert response.status_code == 201
        batches.append(response.get_json())

    workers = []
    for username in ("wc_mong", "wc_annie", "wc_annie_sis", "wc_cherry"):
        password = _create_worker(username, ["work", "pick", "pack", "receive"])
        workers.append((username, _login(client, username, password)))

    claimed = []
    for _username_value, headers in workers:
        response = client.post(
            "/api/work-control/tasks/claim-next",
            headers=headers,
            json={"warehouse_id": 1, "task_types": ["PICKING", "PACKING"]},
        )
        assert response.status_code == 200
        claimed.append(response.get_json()["task"])

    assert len({task["task_id"] for task in claimed}) == 4
    assert [(task["pack_note_ref"], task["task_type"]) for task in claimed] == [
        ("SIM-3440", "PICKING"),
        ("SIM-3440", "PACKING"),
        ("SIM-3441", "PICKING"),
        ("SIM-3441", "PACKING"),
    ]

    first_headers = workers[0][1]
    first_task = claimed[0]
    assert client.post(
        f"/api/work-control/tasks/{first_task['task_id']}/verify-scan",
        headers=first_headers,
        json={"barcode": "SIM-3440"},
    ).status_code == 200
    assert client.post(
        f"/api/work-control/tasks/{first_task['task_id']}/transition",
        headers=first_headers,
        json={"action": "START"},
    ).status_code == 200
    completed = client.post(
        f"/api/work-control/tasks/{first_task['task_id']}/transition",
        headers=first_headers,
        json={
            "action": "COMPLETE",
            "claim_next": True,
            "next_task_types": ["PICKING", "PACKING"],
        },
    )
    assert completed.status_code == 200
    assert completed.get_json()["next_task"]["pack_note_ref"] == "SIM-3442"
    assert completed.get_json()["next_task"]["task_type"] == "PICKING"


def test_cross_check_skips_opposite_stage_but_other_worker_can_claim_it(
    client, auth_headers,
):
    for pack_note in ("CROSS-4100", "CROSS-4101"):
        response = client.post(
            "/api/work-control/batches",
            headers=auth_headers,
            json=_batch_payload(
                2, pack_note_ref=pack_note, declared_order_count=50,
            ),
        )
        assert response.status_code == 201

    first_password = _create_worker(
        "wc_cross_first", ["work", "pick", "pack"],
    )
    second_password = _create_worker(
        "wc_cross_second", ["work", "pick", "pack"],
    )
    first_headers = _login(client, "wc_cross_first", first_password)
    second_headers = _login(client, "wc_cross_second", second_password)

    first_claim = client.post(
        "/api/work-control/tasks/claim-next",
        headers=first_headers,
        json={"warehouse_id": 1, "task_types": ["PICKING", "PACKING"]},
    )
    assert first_claim.status_code == 200
    first_task = first_claim.get_json()["task"]
    assert (first_task["pack_note_ref"], first_task["task_type"]) == (
        "CROSS-4100", "PICKING",
    )

    assert client.post(
        f"/api/work-control/tasks/{first_task['task_id']}/verify-scan",
        headers=first_headers,
        json={"barcode": "CROSS-4100"},
    ).status_code == 200
    assert client.post(
        f"/api/work-control/tasks/{first_task['task_id']}/transition",
        headers=first_headers,
        json={"action": "START"},
    ).status_code == 200
    completed = client.post(
        f"/api/work-control/tasks/{first_task['task_id']}/transition",
        headers=first_headers,
        json={
            "action": "COMPLETE",
            "claim_next": True,
            "next_task_types": ["PICKING", "PACKING"],
        },
    )
    assert completed.status_code == 200
    auto_next = completed.get_json()["next_task"]
    assert (auto_next["pack_note_ref"], auto_next["task_type"]) == (
        "CROSS-4101", "PICKING",
    )

    second_claim = client.post(
        "/api/work-control/tasks/claim-next",
        headers=second_headers,
        json={"warehouse_id": 1, "task_types": ["PICKING", "PACKING"]},
    )
    assert second_claim.status_code == 200
    counterpart = second_claim.get_json()["task"]
    assert (counterpart["pack_note_ref"], counterpart["task_type"]) == (
        "CROSS-4100", "PACKING",
    )


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
