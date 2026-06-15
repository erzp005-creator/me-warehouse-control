"""
Tests for wave picking workflow: validate, create wave batch, combined picks,
short pick distribution, and full wave-to-pack integration.
"""

import pytest

from db_test_context import get_raw_connection


# --- Helpers ---

def _create_extra_so(so_number, customer, items_qty, warehouse_id=1):
    """Create an SO with lines directly in the DB for testing."""
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO sales_orders (so_number, so_barcode, customer_name, status, warehouse_id, created_by, external_id)
           VALUES (%s, %s, %s, 'OPEN', %s, 'admin', gen_random_uuid()) RETURNING so_id""",
        (so_number, so_number, customer, warehouse_id),
    )
    so_id = cur.fetchone()[0]
    for idx, (item_id, qty) in enumerate(items_qty, 1):
        cur.execute(
            """INSERT INTO sales_order_lines (so_id, item_id, quantity_ordered, line_number)
               VALUES (%s, %s, %s, %s)""",
            (so_id, item_id, qty, idx),
        )
    cur.close()
    return so_id


def _set_so_status(so_id, status):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute("UPDATE sales_orders SET status = %s WHERE so_id = %s", (status, so_id))
    cur.close()


def _get_inventory(item_id, bin_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT quantity_on_hand, quantity_allocated FROM inventory WHERE item_id = %s AND bin_id = %s",
        (item_id, bin_id),
    )
    row = cur.fetchone()
    cur.close()
    return row


def _get_wave_breakdowns(pick_task_id):
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT so_id, so_line_id, quantity, quantity_picked, short_quantity FROM wave_pick_breakdown WHERE pick_task_id = %s ORDER BY so_id",
        (pick_task_id,),
    )
    rows = cur.fetchall()
    cur.close()
    return rows


# --- Validation Tests ---


def test_validate_valid_so(client, auth_headers):
    """Valid SO returns valid=true with line count and units."""
    resp = client.post(
        "/api/picking/wave-validate",
        json={"so_barcode": "SO-2026-001", "warehouse_id": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["valid"] is True
    assert data["so_number"] == "SO-2026-001"
    assert data["line_count"] == 1
    assert data["total_units"] == 2  # item 1 qty 2


def test_validate_unknown_so(client, auth_headers):
    """Unknown barcode returns valid=false, order not found."""
    resp = client.post(
        "/api/picking/wave-validate",
        json={"so_barcode": "FAKE-9999", "warehouse_id": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 404
    data = resp.get_json()
    assert data["valid"] is False
    assert "not found" in data["error"]


def test_validate_so_in_active_batch(client, auth_headers):
    """SO already in an active batch returns valid=false with batch_id."""
    # Create a batch with SO-2026-001
    client.post(
        "/api/picking/create-batch",
        json={"so_identifiers": ["SO-2026-001"], "warehouse_id": 1},
        headers=auth_headers,
    )
    # Now try to validate SO-2026-001 again
    resp = client.post(
        "/api/picking/wave-validate",
        json={"so_barcode": "SO-2026-001", "warehouse_id": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 409
    data = resp.get_json()
    assert data["valid"] is False
    assert "already in active pick batch" in data["error"]
    assert "batch_id" in data


def test_validate_so_no_items(client, auth_headers):
    """SO with no line items returns valid=false."""
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO sales_orders (so_number, so_barcode, customer_name, status, warehouse_id, created_by, external_id)
           VALUES ('SO-EMPTY', 'SO-EMPTY', 'Empty Customer', 'OPEN', 1, 'admin', gen_random_uuid())"""
    )
    cur.close()

    resp = client.post(
        "/api/picking/wave-validate",
        json={"so_barcode": "SO-EMPTY", "warehouse_id": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["valid"] is False
    assert "no items" in data["error"].lower()


def test_validate_accepts_new_barcode_field(client, auth_headers):
    """The renamed `barcode` field works alongside the legacy `so_barcode`
    alias. Mobile builds shipped after the rename use `barcode`."""
    resp = client.post(
        "/api/picking/wave-validate",
        json={"barcode": "SO-2026-001", "warehouse_id": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["valid"] is True
    assert data["kind"] == "SO"
    assert data["so_number"] == "SO-2026-001"


# --- TO Validation Tests ---


def _create_to(to_number, source_warehouse_id=1, dest_warehouse_id=2,
               status="OPEN", lines=None):
    """Insert a transfer_orders header + lines. `lines` is a list of
    (item_id, committed_qty) tuples; default = [(1, 2)]."""
    if lines is None:
        lines = [(1, 2)]
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO transfer_orders
              (to_number, source_warehouse_id, destination_warehouse_id,
               status, created_by, external_id)
           VALUES (%s, %s, %s, %s, 'admin', gen_random_uuid())
           RETURNING to_id""",
        (to_number, source_warehouse_id, dest_warehouse_id, status),
    )
    to_id = cur.fetchone()[0]
    line_ids = []
    for idx, (item_id, committed_qty) in enumerate(lines, 1):
        cur.execute(
            """INSERT INTO transfer_order_lines
                  (to_id, item_id, line_number, requested_qty,
                   committed_qty, status)
               VALUES (%s, %s, %s, %s, %s, 'PENDING')
               RETURNING to_line_id""",
            (to_id, item_id, idx, committed_qty, committed_qty),
        )
        line_ids.append(cur.fetchone()[0])
    cur.close()
    return to_id, line_ids


def _provision_to_batch(to_id, to_line_ids, assigned_to="admin",
                        warehouse_id=1, bin_id=2):
    """Create a pick_batches row + pick_tasks for a TO, matching what
    admin start-picking would do."""
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO pick_batches
              (batch_number, warehouse_id, status, assigned_to)
           VALUES (%s, %s, 'OPEN', %s) RETURNING batch_id""",
        (f"BATCH-TO-TEST-{to_id}", warehouse_id, assigned_to),
    )
    batch_id = cur.fetchone()[0]
    for seq, to_line_id in enumerate(to_line_ids, 1):
        cur.execute(
            """SELECT item_id, committed_qty FROM transfer_order_lines
                WHERE to_line_id = %s""",
            (to_line_id,),
        )
        item_id, qty = cur.fetchone()
        cur.execute(
            """INSERT INTO pick_tasks
                  (batch_id, to_id, to_line_id, item_id, bin_id,
                   quantity_to_pick, pick_sequence, status)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 'PENDING')""",
            (batch_id, to_id, to_line_id, item_id, bin_id, qty, seq),
        )
    cur.close()
    return batch_id


def test_validate_to_happy_path(client, auth_headers):
    """A TO assigned to the caller with a provisioned batch returns
    valid=true, kind=TO, with batch_id ready for PickWalk."""
    to_id, line_ids = _create_to("TO-HAPPY-1", lines=[(1, 2)])
    batch_id = _provision_to_batch(to_id, line_ids)

    resp = client.post(
        "/api/picking/wave-validate",
        json={"barcode": "TO-HAPPY-1", "warehouse_id": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["valid"] is True
    assert data["kind"] == "TO"
    assert data["to_id"] == to_id
    assert data["to_number"] == "TO-HAPPY-1"
    assert data["batch_id"] == batch_id
    assert data["line_count"] == 1
    assert data["total_units"] == 2


def test_validate_to_not_started(client, auth_headers):
    """A TO with no pick_batch yet returns 409 with a clear error so the
    operator knows to ask admin to start picking."""
    _create_to("TO-NOTSTARTED")  # no batch provisioned

    resp = client.post(
        "/api/picking/wave-validate",
        json={"barcode": "TO-NOTSTARTED", "warehouse_id": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 409
    data = resp.get_json()
    assert data["valid"] is False
    assert "not started" in data["error"].lower()
    assert data["to_number"] == "TO-NOTSTARTED"


def test_validate_to_assigned_to_other_user(client, auth_headers):
    """A TO whose batch is assigned to a different picker returns 409
    naming that picker, so the scanner doesn't stomp on their work."""
    to_id, line_ids = _create_to("TO-OTHERUSER")
    _provision_to_batch(to_id, line_ids, assigned_to="someone_else")

    resp = client.post(
        "/api/picking/wave-validate",
        json={"barcode": "TO-OTHERUSER", "warehouse_id": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 409
    data = resp.get_json()
    assert data["valid"] is False
    assert data["assigned_to"] == "someone_else"


def test_validate_to_wrong_warehouse(client, auth_headers):
    """A TO at a different source warehouse is treated as not-found at
    the scanner's warehouse (falls through to the 404 path)."""
    to_id, line_ids = _create_to("TO-WRONGWH", source_warehouse_id=2,
                                  dest_warehouse_id=1)
    _provision_to_batch(to_id, line_ids, warehouse_id=2)

    resp = client.post(
        "/api/picking/wave-validate",
        json={"barcode": "TO-WRONGWH", "warehouse_id": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 404
    data = resp.get_json()
    assert data["valid"] is False
    assert "not found" in data["error"].lower()


def test_validate_to_closed_status(client, auth_headers):
    """A CLOSED TO returns 409 with the current status surfaced."""
    _create_to("TO-CLOSED", status="CLOSED")

    resp = client.post(
        "/api/picking/wave-validate",
        json={"barcode": "TO-CLOSED", "warehouse_id": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 409
    data = resp.get_json()
    assert data["valid"] is False
    assert data["to_status"] == "CLOSED"


# --- Wave Create Tests ---


def test_wave_create_single_order(client, auth_headers, seed_data):
    """Wave create with a single SO works like regular batch creation."""
    resp = client.post(
        "/api/picking/wave-create",
        json={"so_ids": [seed_data["so_ids"][0]], "warehouse_id": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total_orders"] == 1
    assert data["total_picks"] > 0
    assert data["total_units"] > 0
    assert "batch_id" in data
    assert data["batch_number"].startswith("WAVE-")


def test_wave_create_multiple_orders(client, auth_headers, seed_data):
    """Wave create combines identical SKUs across orders."""
    # SO-2026-001 has item 1 (qty 2)
    # SO-2026-002 has item 5 (qty 1)
    resp = client.post(
        "/api/picking/wave-create",
        json={"so_ids": seed_data["so_ids"], "warehouse_id": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total_orders"] == 2
    assert data["total_picks"] > 0
    assert data["total_units"] == 3  # 2 + 1
    assert len(data["orders"]) == 2


def test_wave_create_pick_path_order(client, auth_headers, seed_data):
    """Wave picks are sorted by pick_sequence for serpentine walk."""
    resp = client.post(
        "/api/picking/wave-create",
        json={"so_ids": seed_data["so_ids"], "warehouse_id": 1},
        headers=auth_headers,
    )
    data = resp.get_json()
    batch_id = data["batch_id"]

    # Get all tasks
    batch_resp = client.get(f"/api/picking/batch/{batch_id}", headers=auth_headers)
    tasks = batch_resp.get_json()["tasks"]

    # Tasks should be in ascending pick_sequence order
    sequences = [t["pick_sequence"] for t in tasks]
    assert sequences == sorted(sequences)


def test_wave_create_allocation(client, auth_headers, seed_data):
    """Inventory is allocated at wave creation time."""
    # Check inventory before
    before = _get_inventory(1, 3)  # item 1 in bin 2
    assert before[1] == 0  # no allocation

    resp = client.post(
        "/api/picking/wave-create",
        json={"so_ids": [seed_data["so_ids"][0]], "warehouse_id": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 200

    # Check inventory after - should be allocated
    after = _get_inventory(1, 3)
    assert after[1] > 0  # allocation increased


def test_wave_create_partial_inventory(client, auth_headers):
    """Insufficient inventory blocks batch creation with 409 insufficient_coverage.

    Replaces the prior "warning + proceed" behaviour that silently
    under-allocated lines, letting SOs ship short. See picking-optimization
    PR + InsufficientCoverageError docstring.
    """
    so_id = _create_extra_so("SO-BIG", "Big Customer", [(5, 999)])  # item 5 only has 25 in stock

    resp = client.post(
        "/api/picking/wave-create",
        json={"so_ids": [so_id], "warehouse_id": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 409
    data = resp.get_json()
    assert data["error_type"] == "insufficient_coverage"
    assert len(data["unpickable"]) == 1
    entry = data["unpickable"][0]
    assert entry["so_id"] == so_id
    assert entry["so_number"] == "SO-BIG"
    assert any(ln["ordered"] == 999 for ln in entry["lines"])


def test_wave_create_exclude_so_ids_drops_unpickable(client, auth_headers, seed_data):
    """Picker accepts the modal: second call with exclude_so_ids commits the
    pickable subset and returns 200."""
    # Mix one pickable (seed SO) + one unpickable SO
    pickable_so = seed_data["so_ids"][0]
    unpickable_so = _create_extra_so("SO-DROPME", "Drop Customer", [(5, 999)])

    # First call: should 409
    resp = client.post(
        "/api/picking/wave-create",
        json={"so_ids": [pickable_so, unpickable_so], "warehouse_id": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 409
    assert resp.get_json()["error_type"] == "insufficient_coverage"

    # Second call with the unpickable SO excluded: should 200
    resp2 = client.post(
        "/api/picking/wave-create",
        json={
            "so_ids": [pickable_so, unpickable_so],
            "warehouse_id": 1,
            "exclude_so_ids": [unpickable_so],
        },
        headers=auth_headers,
    )
    assert resp2.status_code == 200
    data = resp2.get_json()
    assert data["total_orders"] == 1
    # The committed batch contains only the pickable SO
    assert all(o["so_id"] != unpickable_so for o in data["orders"])


def test_wave_create_duplicate_so(client, auth_headers, seed_data):
    """Duplicate SO IDs in request are rejected."""
    so_id = seed_data["so_ids"][0]
    resp = client.post(
        "/api/picking/wave-create",
        json={"so_ids": [so_id, so_id], "warehouse_id": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 400
    assert "Duplicate" in resp.get_json()["error"]


def test_wave_create_same_user_rescan_auto_cancels_prior(client, auth_headers):
    """No-Resume rule applies to waves too: the same operator
    re-scanning an SO that was in a
    prior wave auto-cancels the prior wave with full revert. Cross-
    user concurrent picking is still protected by the per-SO active-
    batch guard (not exercised here -- single-user fixture)."""
    # Create three OPEN SOs.
    so_a = _create_extra_so("SO-BATCH-A", "Cust A", [(1, 1)])
    so_b = _create_extra_so("SO-BATCH-B", "Cust B", [(2, 1)])
    so_c = _create_extra_so("SO-BATCH-C", "Cust C", [(3, 1)])

    # Create first wave with SO-A.
    first = client.post(
        "/api/picking/wave-create",
        json={"so_ids": [so_a], "warehouse_id": 1},
        headers=auth_headers,
    )
    assert first.status_code == 200
    first_batch_id = first.get_json()["batch_id"]

    # Re-scan including SO-A. Auto-cancel fires on the prior wave,
    # the new wave is created successfully.
    resp = client.post(
        "/api/picking/wave-create",
        json={"so_ids": [so_a, so_c], "warehouse_id": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    new_batch_id = resp.get_json()["batch_id"]
    assert new_batch_id != first_batch_id

    # The prior wave is CANCELLED.
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT status FROM pick_batches WHERE batch_id = %s",
        (first_batch_id,),
    )
    assert cur.fetchone()[0] == "CANCELLED"
    cur.close()


# --- Wave Pick Breakdown Tests ---


def test_breakdown_records_created(client, auth_headers, seed_data):
    """Wave create produces breakdown records for each SO per pick task."""
    resp = client.post(
        "/api/picking/wave-create",
        json={"so_ids": seed_data["so_ids"], "warehouse_id": 1},
        headers=auth_headers,
    )
    data = resp.get_json()
    batch_id = data["batch_id"]

    # Get tasks
    batch_resp = client.get(f"/api/picking/batch/{batch_id}", headers=auth_headers)
    tasks = batch_resp.get_json()["tasks"]

    # Each task should have breakdown records
    total_breakdown = 0
    for task in tasks:
        breakdowns = _get_wave_breakdowns(task["pick_task_id"])
        assert len(breakdowns) > 0
        total_breakdown += len(breakdowns)

    assert total_breakdown >= len(tasks)


def test_breakdown_quantities_sum(client, auth_headers, seed_data):
    """Breakdown quantities per task sum to the task's total quantity."""
    resp = client.post(
        "/api/picking/wave-create",
        json={"so_ids": seed_data["so_ids"], "warehouse_id": 1},
        headers=auth_headers,
    )
    data = resp.get_json()
    batch_id = data["batch_id"]

    batch_resp = client.get(f"/api/picking/batch/{batch_id}", headers=auth_headers)
    tasks = batch_resp.get_json()["tasks"]

    for task in tasks:
        breakdowns = _get_wave_breakdowns(task["pick_task_id"])
        bd_total = sum(b[2] for b in breakdowns)  # quantity column
        assert bd_total == task["quantity_to_pick"]


# --- Next Task with Contributing Orders ---


def test_next_task_has_contributing_orders(client, auth_headers, seed_data):
    """Next task endpoint includes contributing_orders for wave picks."""
    resp = client.post(
        "/api/picking/wave-create",
        json={"so_ids": seed_data["so_ids"], "warehouse_id": 1},
        headers=auth_headers,
    )
    batch_id = resp.get_json()["batch_id"]

    next_resp = client.get(f"/api/picking/batch/{batch_id}/next", headers=auth_headers)
    data = next_resp.get_json()
    assert "contributing_orders" in data
    assert "pick_number" in data
    assert "total_picks" in data
    assert data["pick_number"] == 1


# --- Short Pick Distribution Tests ---


def test_short_pick_fifo_distribution(client, auth_headers):
    """Short pick distributes shortage to later SOs (FIFO by SO ID)."""
    # Create two SOs that share item 1 (TST-001, 50 in stock)
    so_a = _create_extra_so("SO-SHORT-A", "Customer A", [(1, 3)])
    so_b = _create_extra_so("SO-SHORT-B", "Customer B", [(1, 5)])

    resp = client.post(
        "/api/picking/wave-create",
        json={"so_ids": [so_a, so_b], "warehouse_id": 1},
        headers=auth_headers,
    )
    data = resp.get_json()
    batch_id = data["batch_id"]

    # Get the pick task for item 1
    batch_resp = client.get(f"/api/picking/batch/{batch_id}", headers=auth_headers)
    tasks = batch_resp.get_json()["tasks"]
    item1_task = [t for t in tasks if t["sku"] == "TST-001"][0]

    # Short pick: only 5 available out of 8 needed (3+5)
    resp = client.post(
        "/api/picking/short",
        json={"pick_task_id": item1_task["pick_task_id"], "quantity_available": 5},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.get_json()["task"]["shortage"] == 3

    # Check breakdown: SO-A (lower ID) should get full 3, SO-B gets 2 (shorted 3)
    breakdowns = _get_wave_breakdowns(item1_task["pick_task_id"])
    # Sort by so_id to ensure FIFO
    breakdowns.sort(key=lambda b: b[0])

    # First SO (lower ID) gets full allocation
    assert breakdowns[0][3] == 3  # quantity_picked
    assert breakdowns[0][4] == 0  # short_quantity

    # Second SO gets remainder
    assert breakdowns[1][3] == 2  # quantity_picked
    assert breakdowns[1][4] == 3  # short_quantity


def test_short_pick_full_shortage(client, auth_headers):
    """Full shortage zeros all breakdown records."""
    so_a = _create_extra_so("SO-ZERO-A", "Customer A", [(2, 2)])
    so_b = _create_extra_so("SO-ZERO-B", "Customer B", [(2, 3)])

    resp = client.post(
        "/api/picking/wave-create",
        json={"so_ids": [so_a, so_b], "warehouse_id": 1},
        headers=auth_headers,
    )
    batch_id = resp.get_json()["batch_id"]

    batch_resp = client.get(f"/api/picking/batch/{batch_id}", headers=auth_headers)
    tasks = batch_resp.get_json()["tasks"]
    task = tasks[0]

    # Short pick: 0 available
    resp = client.post(
        "/api/picking/short",
        json={"pick_task_id": task["pick_task_id"], "quantity_available": 0},
        headers=auth_headers,
    )
    assert resp.status_code == 200

    breakdowns = _get_wave_breakdowns(task["pick_task_id"])
    for bd in breakdowns:
        assert bd[3] == 0  # quantity_picked = 0
        assert bd[4] == bd[2]  # short_quantity = original quantity


# --- Confirm Pick with Wave Breakdown ---


def test_confirm_wave_pick_updates_all_so_lines(client, auth_headers):
    """Confirming a wave pick updates quantity_picked on all contributing SO lines."""
    so_a = _create_extra_so("SO-CONF-A", "Customer A", [(1, 2)])
    so_b = _create_extra_so("SO-CONF-B", "Customer B", [(1, 4)])

    resp = client.post(
        "/api/picking/wave-create",
        json={"so_ids": [so_a, so_b], "warehouse_id": 1},
        headers=auth_headers,
    )
    batch_id = resp.get_json()["batch_id"]

    batch_resp = client.get(f"/api/picking/batch/{batch_id}", headers=auth_headers)
    tasks = batch_resp.get_json()["tasks"]
    task = [t for t in tasks if t["sku"] == "TST-001"][0]

    # Confirm pick with barcode
    resp = client.post(
        "/api/picking/confirm",
        json={
            "pick_task_id": task["pick_task_id"],
            "scanned_barcode": "100000000001",
            "quantity_picked": 6,
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200

    # Check breakdowns are all marked as picked
    breakdowns = _get_wave_breakdowns(task["pick_task_id"])
    total_picked = sum(b[3] for b in breakdowns)
    assert total_picked == 6

    # Check SO lines are updated
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute("SELECT quantity_picked FROM sales_order_lines WHERE so_id = %s", (so_a,))
    assert cur.fetchone()[0] == 2
    cur.execute("SELECT quantity_picked FROM sales_order_lines WHERE so_id = %s", (so_b,))
    assert cur.fetchone()[0] == 4
    cur.close()


# --- Integration Tests ---


def test_wave_pick_to_pack_flow(client, auth_headers):
    """Full flow: scan SOs, create wave, pick all, complete batch, pack each SO."""
    # Create 3 SOs with some shared items
    so1 = _create_extra_so("SO-FLOW-1", "Cust 1", [(1, 2), (6, 1)])
    so2 = _create_extra_so("SO-FLOW-2", "Cust 2", [(1, 3), (3, 2)])
    so3 = _create_extra_so("SO-FLOW-3", "Cust 3", [(6, 2)])

    # Validate each
    for barcode in ["SO-FLOW-1", "SO-FLOW-2", "SO-FLOW-3"]:
        resp = client.post(
            "/api/picking/wave-validate",
            json={"so_barcode": barcode, "warehouse_id": 1},
            headers=auth_headers,
        )
        assert resp.get_json()["valid"] is True

    # Create wave
    resp = client.post(
        "/api/picking/wave-create",
        json={"so_ids": [so1, so2, so3], "warehouse_id": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.get_json()
    batch_id = data["batch_id"]
    assert data["total_orders"] == 3
    assert data["total_units"] == 10  # 2+1+3+2+2

    # Pick all tasks
    while True:
        next_resp = client.get(f"/api/picking/batch/{batch_id}/next", headers=auth_headers)
        next_data = next_resp.get_json()
        if "message" in next_data:
            break
        # Confirm pick
        client.post(
            "/api/picking/confirm",
            json={
                "pick_task_id": next_data["pick_task_id"],
                "scanned_barcode": next_data["upc"],
                "quantity_picked": next_data["quantity_to_pick"],
            },
            headers=auth_headers,
        )

    # Complete batch
    resp = client.post(
        "/api/picking/complete-batch",
        json={"batch_id": batch_id},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    summary = resp.get_json()["summary"]
    assert summary["total_orders"] == 3
    assert summary["total_shorts"] == 0

    # Verify each SO is now in PICKED state and can be loaded by packing.
    for so_id, barcode in [(so1, "SO-FLOW-1"), (so2, "SO-FLOW-2"), (so3, "SO-FLOW-3")]:
        pack_resp = client.get(f"/api/packing/order/{barcode}", headers=auth_headers)
        assert pack_resp.status_code == 200


def test_wave_pick_with_shorts_to_pack(client, auth_headers):
    """Wave pick with operator-confirmed short picks still completes the
    batch. The short_pick path leaves a wave_pick_breakdown.short_quantity
    marker that the complete_batch Layer 2A guard recognises as a
    legitimate close-out, so the SO can still move to PICKED."""
    # Two SOs that fully fit (item 5 has 25 in stock; demand totals 10).
    so1 = _create_extra_so("SO-SHRT-1", "Cust 1", [(5, 6)])
    so2 = _create_extra_so("SO-SHRT-2", "Cust 2", [(5, 4)])

    resp = client.post(
        "/api/picking/wave-create",
        json={"so_ids": [so1, so2], "warehouse_id": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.get_json()
    batch_id = data["batch_id"]

    # First wave task: short-pick at half the requested quantity. This
    # leaves wave_pick_breakdown rows with short_quantity > 0 - the
    # explicit operator-confirmed shortfall marker.
    first = client.get(f"/api/picking/batch/{batch_id}/next", headers=auth_headers).get_json()
    short_resp = client.post(
        "/api/picking/short",
        json={"pick_task_id": first["pick_task_id"], "quantity_available": first["quantity_to_pick"] // 2},
        headers=auth_headers,
    )
    assert short_resp.status_code == 200

    # Pick everything else normally.
    while True:
        nx = client.get(f"/api/picking/batch/{batch_id}/next", headers=auth_headers).get_json()
        if "message" in nx:
            break
        client.post(
            "/api/picking/confirm",
            json={
                "pick_task_id": nx["pick_task_id"],
                "scanned_barcode": nx["upc"],
                "quantity_picked": nx["quantity_to_pick"],
            },
            headers=auth_headers,
        )

    complete_resp = client.post(
        "/api/picking/complete-batch",
        json={"batch_id": batch_id},
        headers=auth_headers,
    )
    assert complete_resp.status_code == 200, complete_resp.get_json()

    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute("SELECT status FROM sales_orders WHERE so_id = %s", (so1,))
    assert cur.fetchone()[0] == "PICKED"
    cur.execute("SELECT status FROM sales_orders WHERE so_id = %s", (so2,))
    assert cur.fetchone()[0] == "PICKED"
    cur.close()
