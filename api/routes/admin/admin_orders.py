"""Purchase Orders, Sales Orders, and Short Picks endpoints."""

import math
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import g, jsonify, request
from sqlalchemy import text

from constants import (
    PO_OPEN, PO_PARTIAL, PO_RECEIVED, PO_CLOSED, PO_ARCHIVED,
    SO_OPEN, SO_PICKED, SO_PACKED, SO_SHIPPED, SO_CANCELLED,
    SO_FRAUD_REVIEW, SO_WAITING_STOCK,
    TASK_PENDING, ADJ_PENDING, ADJ_APPROVED, ADJ_REASON_SHORT,
    ORDER_TYPE_BACKORDER,
    CANCEL_REASONS, CANCEL_REASON_OTHER,
    COMPANY_TIMEZONE,
    ACTION_PICK,
    ACTION_PO_STATUS_CHANGED,
    ACTION_PO_LINE_ADDED,
    ACTION_PO_LINE_UPDATED,
    ACTION_PO_LINE_REMOVED,
    ACTION_SO_ADDRESS_EDITED,
    ACTION_SO_FRAUD_CLEARED,
    ACTION_SO_MEMO_EDITED,
    ACTION_SO_HEADER_EDITED,
    ACTION_SO_LINE_ADDED,
    ACTION_SO_LINE_UPDATED,
    ACTION_SO_LINE_REMOVED,
    ACTION_SO_SOURCE_SYSTEM_REASSIGNED,
    ACTION_SO_ALLOCATION_RELEASED,
    ACTION_PARTIAL_FULFILL,
    ACTION_BACKORDER_CANCELLED,
    OVERRIDE_SO_FULL_EDIT,
    ROLE_ADMIN,
)
from middleware.auth_middleware import (
    has_override,
    require_admin_or_page_permission,
    require_auth,
)
from middleware.db import with_db
from routes.admin import admin_bp
from schemas.purchase_orders import (
    AddPOLineRequest,
    CreatePurchaseOrderRequest,
    UpdatePOLineRequest,
    UpdatePurchaseOrderRequest,
)
from schemas.sales_orders import (
    ADDRESS_FIELD_NAMES,
    AddSalesOrderLineRequest,
    AdminPickRequest,
    CancelBackorderRequest,
    CreateSalesOrderRequest,
    MarkSalesOrdersPrintedRequest,
    PartialFulfillRequest,
    RevertSalesOrderStatusRequest,
    UpdateSalesOrderAddressRequest,
    UpdateSalesOrderLineRequest,
    UpdateSalesOrderMemoRequest,
    UpdateSalesOrderRequest,
)
from services.audit_service import write_audit_log
from services.events_service import emit_event, get_user_external_id
from services.sales_order_service import (
    AdminPickError,
    CancelNotAllowed,
    RevertNotAllowed,
    cancel_sales_order as _cancel_so,
    maybe_promote_so_to_picked,
    record_admin_pick,
    revert_sales_order_status as _revert_so_status,
)
from utils.validation import validate_body


# ── Purchase Orders ───────────────────────────────────────────────────────────

@admin_bp.route("/purchase-orders", methods=["GET"])
@require_auth
@require_admin_or_page_permission("purchase-orders")
@with_db
def list_purchase_orders():
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 50, type=int), 1000)

    where_clauses, params = [], {}
    status = request.args.get("status")
    warehouse_id = request.args.get("warehouse_id", type=int)
    search = (request.args.get("q") or "").strip()
    # ARCHIVED is hidden by default so the active list stays focused
    # on POs that still need work. Caller opts in by passing
    # include_archived=true OR by setting status=ARCHIVED explicitly.
    include_archived = request.args.get("include_archived", "false").lower() == "true"
    if status:
        where_clauses.append("status = :status")
        params["status"] = status
    elif not include_archived:
        where_clauses.append("status <> :archived")
        params["archived"] = PO_ARCHIVED
    if warehouse_id:
        where_clauses.append("warehouse_id = :wid")
        params["wid"] = warehouse_id
    if search:
        where_clauses.append("(po_number ILIKE :q OR vendor_name ILIKE :q)")
        params["q"] = f"%{search}%"

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    total = g.db.execute(text(f"SELECT COUNT(*) FROM purchase_orders {where_sql}"), params).scalar()
    pages = max(1, math.ceil(total / per_page))

    params["limit"] = per_page
    params["offset"] = (page - 1) * per_page
    rows = g.db.execute(
        text(f"""
            SELECT po_id, po_number, po_barcode, vendor_name, status, expected_date,
                   warehouse_id, notes, created_at, received_at, created_by
            FROM purchase_orders {where_sql} ORDER BY po_id DESC LIMIT :limit OFFSET :offset
        """),
        params,
    ).fetchall()

    return jsonify({
        "purchase_orders": [
            {"po_id": r.po_id, "po_number": r.po_number, "po_barcode": r.po_barcode,
             "vendor_name": r.vendor_name, "status": r.status,
             "expected_date": r.expected_date.isoformat() if r.expected_date else None,
             "warehouse_id": r.warehouse_id, "notes": r.notes,
             "created_at": r.created_at.isoformat() if r.created_at else None,
             "received_at": r.received_at.isoformat() if r.received_at else None,
             "created_by": r.created_by}
            for r in rows
        ],
        "total": total, "page": page, "per_page": per_page, "pages": pages,
    })


@admin_bp.route("/purchase-orders/<int:po_id>", methods=["GET"])
@require_auth
@require_admin_or_page_permission("purchase-orders")
@with_db
def get_purchase_order(po_id):
    po = g.db.execute(
        text("SELECT po_id, po_number, po_barcode, vendor_name, vendor_id, status, expected_date, warehouse_id, notes, created_at, received_at, created_by FROM purchase_orders WHERE po_id = :pid"),
        {"pid": po_id},
    ).fetchone()
    if not po:
        return jsonify({"error": "Purchase order not found"}), 404

    lines = g.db.execute(
        text("""
            SELECT pol.po_line_id, pol.line_number, pol.item_id, i.sku, i.item_name, i.upc,
                   pol.quantity_ordered, pol.quantity_received, pol.unit_cost, pol.status
            FROM purchase_order_lines pol JOIN items i ON i.item_id = pol.item_id
            WHERE pol.po_id = :pid ORDER BY pol.line_number
        """),
        {"pid": po_id},
    ).fetchall()

    return jsonify({
        "purchase_order": {
            "po_id": po.po_id, "po_number": po.po_number, "po_barcode": po.po_barcode,
            "vendor_name": po.vendor_name, "status": po.status,
            "expected_date": po.expected_date.isoformat() if po.expected_date else None,
            "warehouse_id": po.warehouse_id, "notes": po.notes,
            "created_at": po.created_at.isoformat() if po.created_at else None,
        },
        "lines": [
            {"po_line_id": l.po_line_id, "line_number": l.line_number, "item_id": l.item_id,
             "sku": l.sku, "item_name": l.item_name, "upc": l.upc,
             "quantity_ordered": l.quantity_ordered, "quantity_received": l.quantity_received,
             "unit_cost": float(l.unit_cost) if l.unit_cost else None, "status": l.status}
            for l in lines
        ],
    })


@admin_bp.route("/purchase-orders", methods=["POST"])
@require_auth
@require_admin_or_page_permission("purchase-orders")
@validate_body(CreatePurchaseOrderRequest)
@with_db
def create_purchase_order(validated):
    data = validated.model_dump()

    dup = g.db.execute(text("SELECT 1 FROM purchase_orders WHERE po_number = :pn"), {"pn": data["po_number"]}).fetchone()
    if dup:
        return jsonify({"error": f"Duplicate po_number: {data['po_number']}"}), 400

    # Validate items exist in DB
    for line in data["lines"]:
        item = g.db.execute(text("SELECT 1 FROM items WHERE item_id = :iid"), {"iid": line["item_id"]}).fetchone()
        if not item:
            return jsonify({"error": f"Item {line['item_id']} not found"}), 400

    result = g.db.execute(
        text("""
            INSERT INTO purchase_orders (po_number, po_barcode, vendor_name, expected_date, warehouse_id, notes, created_by, status, external_id)
            VALUES (:pn, :pb, :vendor, :exp_date, :wid, :notes, :created_by, :status, :ext_id)
            RETURNING po_id
        """),
        {
            "pn": data["po_number"], "pb": data.get("po_barcode", data["po_number"]),
            "vendor": data.get("vendor_name"), "exp_date": data.get("expected_date"),
            "wid": data["warehouse_id"], "notes": data.get("notes"),
            "created_by": g.current_user["username"], "status": PO_OPEN,
            "ext_id": str(uuid.uuid4()),
        },
    )
    po_id = result.fetchone()[0]

    for line in data["lines"]:
        g.db.execute(
            text("INSERT INTO purchase_order_lines (po_id, item_id, quantity_ordered, unit_cost, line_number) VALUES (:pid, :iid, :qty, :cost, :ln)"),
            {"pid": po_id, "iid": line["item_id"], "qty": line["quantity_ordered"],
             "cost": float(line["unit_cost"]) if line.get("unit_cost") is not None else None,
             "ln": line.get("line_number") or 1},
        )

    g.db.commit()

    # Re-fetch to return (save/restore g.db since get_purchase_order has @with_db)
    outer_db = g.db
    response = get_purchase_order(po_id)
    g.db = outer_db
    return response


PO_STATUS_VALUES = {PO_OPEN, PO_PARTIAL, PO_RECEIVED, PO_CLOSED, PO_ARCHIVED}
PO_EDIT_BLOCKED_STATUSES = {PO_CLOSED, PO_ARCHIVED}


@admin_bp.route("/purchase-orders/<int:po_id>", methods=["PUT"])
@require_auth
@require_admin_or_page_permission("purchase-orders")
@validate_body(UpdatePurchaseOrderRequest)
@with_db
def update_purchase_order(po_id, validated):
    data = validated.model_dump(exclude_unset=True)

    po = g.db.execute(
        text("SELECT po_id, status, warehouse_id FROM purchase_orders WHERE po_id = :pid"),
        {"pid": po_id},
    ).fetchone()
    if not po:
        return jsonify({"error": "Purchase order not found"}), 404

    new_status = data.get("status")
    if new_status is not None:
        if new_status not in PO_STATUS_VALUES:
            return jsonify({"error": f"Invalid status: {new_status}"}), 400
        # ARCHIVED is reachable only from CLOSED so an active PO cannot
        # be hidden by a single dropdown change. All other transitions
        # (including ARCHIVED -> any other) are allowed; admins use the
        # dropdown to un-archive when needed.
        if new_status == PO_ARCHIVED and po.status != PO_CLOSED:
            return jsonify({
                "error": "PO must be CLOSED before it can be ARCHIVED",
            }), 400

    HEADER_FIELDS = {"po_number", "po_barcode", "vendor_name", "expected_date", "notes"}
    header_data = {k: v for k, v in data.items() if k in HEADER_FIELDS}
    # Header edits are blocked when the PO is locked (CLOSED or
    # ARCHIVED). The status field itself is still editable so an admin
    # can transition out of CLOSED before resuming line work.
    if header_data and po.status in PO_EDIT_BLOCKED_STATUSES:
        return jsonify({
            "error": f"Header edits are not allowed while the PO is {po.status}",
        }), 400

    set_fragments, params = [], {"pid": po_id}
    for col, val in header_data.items():
        set_fragments.append(f"{col} = :{col}")
        params[col] = val
    if new_status is not None and new_status != po.status:
        set_fragments.append("status = :status")
        params["status"] = new_status

    if not set_fragments:
        return jsonify({"error": "No valid fields provided"}), 400

    g.db.execute(
        text(f"UPDATE purchase_orders SET {', '.join(set_fragments)} WHERE po_id = :pid"),
        params,
    )

    if new_status is not None and new_status != po.status:
        write_audit_log(
            g.db,
            ACTION_PO_STATUS_CHANGED,
            "PO",
            po_id,
            g.current_user["user_id"],
            po.warehouse_id,
            details={"old_status": po.status, "new_status": new_status},
        )

    g.db.commit()

    row = g.db.execute(
        text("SELECT po_id, po_number, po_barcode, vendor_name, status, expected_date, warehouse_id, notes, created_at FROM purchase_orders WHERE po_id = :pid"),
        {"pid": po_id},
    ).fetchone()
    return jsonify({
        "po_id": row.po_id, "po_number": row.po_number, "po_barcode": row.po_barcode,
        "vendor_name": row.vendor_name, "status": row.status,
        "expected_date": row.expected_date.isoformat() if row.expected_date else None,
        "warehouse_id": row.warehouse_id, "notes": row.notes,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    })


# ── Purchase Order Lines (add / update / remove) ─────────────────────────────

@admin_bp.route("/purchase-orders/<int:po_id>/lines", methods=["POST"])
@require_auth
@require_admin_or_page_permission("purchase-orders")
@validate_body(AddPOLineRequest)
@with_db
def add_purchase_order_line(po_id, validated):
    data = validated.model_dump()

    po = g.db.execute(
        text("SELECT po_id, status, warehouse_id FROM purchase_orders WHERE po_id = :pid"),
        {"pid": po_id},
    ).fetchone()
    if not po:
        return jsonify({"error": "Purchase order not found"}), 404
    if po.status in PO_EDIT_BLOCKED_STATUSES:
        return jsonify({
            "error": f"Cannot add lines while the PO is {po.status}",
        }), 400

    item = g.db.execute(
        text("SELECT item_id, sku FROM items WHERE item_id = :iid"),
        {"iid": data["item_id"]},
    ).fetchone()
    if not item:
        return jsonify({"error": f"Item {data['item_id']} not found"}), 400

    # Reject duplicate items on the same PO; the receive endpoint
    # already does LIMIT 1 against (po_id, item_id) and would not be
    # able to disambiguate two lines with the same item_id.
    dup = g.db.execute(
        text("SELECT 1 FROM purchase_order_lines WHERE po_id = :pid AND item_id = :iid"),
        {"pid": po_id, "iid": data["item_id"]},
    ).fetchone()
    if dup:
        return jsonify({"error": f"Item {item.sku} is already on this PO"}), 400

    next_ln = g.db.execute(
        text("SELECT COALESCE(MAX(line_number), 0) + 1 FROM purchase_order_lines WHERE po_id = :pid"),
        {"pid": po_id},
    ).scalar()

    result = g.db.execute(
        text(
            """
            INSERT INTO purchase_order_lines
                (po_id, item_id, quantity_ordered, unit_cost, line_number)
            VALUES (:pid, :iid, :qty, :cost, :ln)
            RETURNING po_line_id
            """
        ),
        {
            "pid": po_id, "iid": data["item_id"],
            "qty": data["quantity_ordered"],
            "cost": float(data["unit_cost"]) if data.get("unit_cost") is not None else None,
            "ln": next_ln,
        },
    )
    po_line_id = result.fetchone()[0]

    write_audit_log(
        g.db,
        ACTION_PO_LINE_ADDED,
        "PO_LINE",
        po_line_id,
        g.current_user["user_id"],
        po.warehouse_id,
        details={
            "po_id": po_id, "sku": item.sku,
            "quantity_ordered": data["quantity_ordered"],
        },
    )
    g.db.commit()

    return jsonify({
        "po_line_id": po_line_id,
        "po_id": po_id,
        "item_id": data["item_id"],
        "sku": item.sku,
        "quantity_ordered": data["quantity_ordered"],
        "quantity_received": 0,
        "line_number": next_ln,
    }), 201


@admin_bp.route("/purchase-orders/<int:po_id>/lines/<int:po_line_id>", methods=["PATCH"])
@require_auth
@require_admin_or_page_permission("purchase-orders")
@validate_body(UpdatePOLineRequest)
@with_db
def update_purchase_order_line(po_id, po_line_id, validated):
    data = validated.model_dump()

    po = g.db.execute(
        text("SELECT po_id, status, warehouse_id FROM purchase_orders WHERE po_id = :pid"),
        {"pid": po_id},
    ).fetchone()
    if not po:
        return jsonify({"error": "Purchase order not found"}), 404
    if po.status in PO_EDIT_BLOCKED_STATUSES:
        return jsonify({
            "error": f"Cannot edit lines while the PO is {po.status}",
        }), 400

    line = g.db.execute(
        text(
            """
            SELECT pol.po_line_id, pol.quantity_ordered, pol.quantity_received, i.sku
            FROM purchase_order_lines pol JOIN items i ON i.item_id = pol.item_id
            WHERE pol.po_line_id = :plid AND pol.po_id = :pid
            """
        ),
        {"plid": po_line_id, "pid": po_id},
    ).fetchone()
    if not line:
        return jsonify({"error": "Line not found"}), 404

    new_qty = data["quantity_ordered"]
    # Blocking ordered<received keeps variance non-negative. The
    # operator either reverses receipts first or leaves the line as-is.
    if new_qty < line.quantity_received:
        return jsonify({
            "error": (
                f"quantity_ordered ({new_qty}) cannot be less than "
                f"quantity_received ({line.quantity_received})"
            ),
        }), 400

    g.db.execute(
        text("UPDATE purchase_order_lines SET quantity_ordered = :qty WHERE po_line_id = :plid"),
        {"qty": new_qty, "plid": po_line_id},
    )

    write_audit_log(
        g.db,
        ACTION_PO_LINE_UPDATED,
        "PO_LINE",
        po_line_id,
        g.current_user["user_id"],
        po.warehouse_id,
        details={
            "po_id": po_id, "sku": line.sku,
            "old_quantity_ordered": line.quantity_ordered,
            "new_quantity_ordered": new_qty,
        },
    )
    g.db.commit()
    return jsonify({"po_line_id": po_line_id, "quantity_ordered": new_qty})


@admin_bp.route("/purchase-orders/<int:po_id>/lines/<int:po_line_id>", methods=["DELETE"])
@require_auth
@require_admin_or_page_permission("purchase-orders")
@with_db
def delete_purchase_order_line(po_id, po_line_id):
    po = g.db.execute(
        text("SELECT po_id, status, warehouse_id FROM purchase_orders WHERE po_id = :pid"),
        {"pid": po_id},
    ).fetchone()
    if not po:
        return jsonify({"error": "Purchase order not found"}), 404
    if po.status in PO_EDIT_BLOCKED_STATUSES:
        return jsonify({
            "error": f"Cannot remove lines while the PO is {po.status}",
        }), 400

    line = g.db.execute(
        text(
            """
            SELECT pol.po_line_id, pol.quantity_ordered, pol.quantity_received, i.sku
            FROM purchase_order_lines pol JOIN items i ON i.item_id = pol.item_id
            WHERE pol.po_line_id = :plid AND pol.po_id = :pid
            """
        ),
        {"plid": po_line_id, "pid": po_id},
    ).fetchone()
    if not line:
        return jsonify({"error": "Line not found"}), 404

    # Blocking removal of lines with received qty preserves inventory
    # history. To remove, operator first reverses receipts via a
    # separate workflow; the soft-close-by-setting-qty-equal-to-
    # received path is intentionally not exposed here.
    if line.quantity_received > 0:
        return jsonify({
            "error": (
                f"Line has {line.quantity_received} received units; "
                "remove the receipts before deleting the line"
            ),
        }), 400

    g.db.execute(
        text("DELETE FROM purchase_order_lines WHERE po_line_id = :plid"),
        {"plid": po_line_id},
    )

    write_audit_log(
        g.db,
        ACTION_PO_LINE_REMOVED,
        "PO_LINE",
        po_line_id,
        g.current_user["user_id"],
        po.warehouse_id,
        details={
            "po_id": po_id, "sku": line.sku,
            "quantity_ordered": line.quantity_ordered,
        },
    )
    g.db.commit()
    return jsonify({"po_line_id": po_line_id, "deleted": True})


@admin_bp.route("/purchase-orders/<int:po_id>/close", methods=["POST"])
@require_auth
@require_admin_or_page_permission("purchase-orders")
@with_db
def close_purchase_order(po_id):
    po = g.db.execute(
        text("SELECT po_id, status FROM purchase_orders WHERE po_id = :pid"),
        {"pid": po_id},
    ).fetchone()
    if not po:
        return jsonify({"error": "Purchase order not found"}), 404
    if po.status == PO_CLOSED:
        return jsonify({"error": "Purchase order is already CLOSED"}), 409

    g.db.execute(
        text("UPDATE purchase_orders SET status = :status WHERE po_id = :pid"),
        {"pid": po_id, "status": PO_CLOSED},
    )
    g.db.commit()
    return jsonify({"message": "Purchase order closed", "status": PO_CLOSED})


@admin_bp.route("/purchase-orders/<int:po_id>/reopen", methods=["POST"])
@require_auth
@require_admin_or_page_permission("purchase-orders")
@with_db
def reopen_purchase_order(po_id):
    po = g.db.execute(
        text("SELECT po_id, status FROM purchase_orders WHERE po_id = :pid"),
        {"pid": po_id},
    ).fetchone()
    if not po:
        return jsonify({"error": "Purchase order not found"}), 404
    if po.status != PO_CLOSED:
        return jsonify({
            "error": f"Only CLOSED purchase orders can be reopened. Current status: {po.status}"
        }), 409

    g.db.execute(
        text("UPDATE purchase_orders SET status = :status WHERE po_id = :pid"),
        {"pid": po_id, "status": PO_OPEN},
    )
    g.db.commit()
    return jsonify({"message": "Purchase order reopened", "status": PO_OPEN})


# ── Source Systems ────────────────────────────────────────────────────────────

@admin_bp.route("/source-systems", methods=["GET"])
@require_auth
@require_admin_or_page_permission("sales-orders")
@with_db
def list_source_systems():
    """Read-only list of canonical source_system
    tags for the SO edit modal's source_system picker.

    /admin/scope-catalog already serves the same list but is gated by
    api-tokens, which the sales-orders operator role does not get by
    default. This focused endpoint keeps the SO edit surface usable
    without granting the broader token permission.
    """
    rows = g.db.execute(
        text(
            "SELECT source_system, kind "
            "  FROM inbound_source_systems_allowlist "
            " ORDER BY source_system"
        )
    ).fetchall()
    return jsonify({
        "source_systems": [
            {"source_system": r.source_system, "kind": r.kind}
            for r in rows
        ],
    })


# ── Sales Orders ──────────────────────────────────────────────────────────────

@admin_bp.route("/sales-orders", methods=["GET"])
@require_auth
@require_admin_or_page_permission("sales-orders")
@with_db
def list_sales_orders():
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 50, type=int), 1000)

    where_clauses, params = [], {}
    status = request.args.get("status")
    warehouse_id = request.args.get("warehouse_id", type=int)
    search = (request.args.get("q") or "").strip()
    # Opt-in filter from the Picking Tickets queue. When set, drops SOs
    # whose ticket was already confirm-rendered to the operator (mig
    # 057 printed_at). Pages that show the full SO ledger (Sales
    # Orders, Fraud, POS Activity) leave the param unset.
    hide_printed = request.args.get("hide_printed", "false").lower() == "true"
    # Opt-in primary-bin computation for the Picking Tickets queue.
    # When set, each row carries the bin_code + pick_sequence of the
    # preferred bin with the LOWEST pick_sequence among the SO's
    # line items, scoped to the SO's warehouse. The list page sorts
    # by pick_sequence so a picker walks the warehouse in physical
    # order; the shipper unpacks the cart in the same order.
    include_primary_bin = request.args.get("include_primary_bin", "false").lower() == "true"
    if status:
        where_clauses.append("so.status = :status")
        params["status"] = status
    if warehouse_id:
        where_clauses.append("so.warehouse_id = :wid")
        params["wid"] = warehouse_id
    if hide_printed:
        where_clauses.append("so.printed_at IS NULL")
    if search:
        where_clauses.append("(so.so_number ILIKE :q OR so.customer_name ILIKE :q)")
        params["q"] = f"%{search}%"

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    total = g.db.execute(
        text(f"SELECT COUNT(*) FROM sales_orders so {where_sql}"),
        params,
    ).scalar()
    pages = max(1, math.ceil(total / per_page))

    params["limit"] = per_page
    params["offset"] = (page - 1) * per_page
    # Primary-bin subqueries only render when the caller asks for them;
    # other pages avoid the per-row scalar subquery cost. Both columns
    # come from the same logical row, so the planner can fold them
    # into one index scan even though they read as two subqueries.
    primary_bin_select = ""
    if include_primary_bin:
        primary_bin_select = """
            , (SELECT b.bin_code
                 FROM sales_order_lines sol
                 JOIN preferred_bins pb ON pb.item_id = sol.item_id
                 JOIN bins b ON b.bin_id = pb.bin_id
                WHERE sol.so_id = so.so_id
                  AND b.warehouse_id = so.warehouse_id
                ORDER BY b.pick_sequence ASC NULLS LAST, pb.priority ASC
                LIMIT 1) AS primary_bin_code
            , (SELECT b.pick_sequence
                 FROM sales_order_lines sol
                 JOIN preferred_bins pb ON pb.item_id = sol.item_id
                 JOIN bins b ON b.bin_id = pb.bin_id
                WHERE sol.so_id = so.so_id
                  AND b.warehouse_id = so.warehouse_id
                ORDER BY b.pick_sequence ASC NULLS LAST, pb.priority ASC
                LIMIT 1) AS primary_bin_pick_sequence
        """
    rows = g.db.execute(
        text(f"""
            SELECT so.so_id, so.so_number, so.so_barcode, so.customer_name, so.customer_phone, so.customer_address,
                   so.status, so.priority, so.warehouse_id,
                   so.ship_method, so.ship_address, so.order_date, so.ship_by_date, so.created_at, so.created_by,
                   so.carrier, so.tracking_number, so.shipped_at,
                   (so.shipped_at AT TIME ZONE :company_tz)::date AS shipped_date_local,
                   so.memo, so.printed_at,
                   so.billing_address_line1, so.billing_address_line2,
                   so.billing_address_city, so.billing_address_state,
                   so.billing_address_postal_code,
                   so.shipping_address_line1, so.shipping_address_line2,
                   so.shipping_address_city, so.shipping_address_state,
                   so.shipping_address_postal_code
                   {primary_bin_select}
            FROM sales_orders so {where_sql}
            ORDER BY so.so_id DESC LIMIT :limit OFFSET :offset
        """),
        {**params, "company_tz": COMPANY_TIMEZONE},
    ).fetchall()

    def _row_to_dict(r):
        out = {
            "so_id": r.so_id, "so_number": r.so_number, "so_barcode": r.so_barcode,
            "customer_name": r.customer_name, "customer_phone": r.customer_phone,
            "customer_address": r.customer_address,
            "status": r.status, "priority": r.priority,
            "warehouse_id": r.warehouse_id, "ship_method": r.ship_method,
            "ship_address": r.ship_address,
            "order_date": r.order_date.isoformat() if r.order_date else None,
            "ship_by_date": r.ship_by_date.isoformat() if r.ship_by_date else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "created_by": r.created_by,
            "carrier": r.carrier, "tracking_number": r.tracking_number,
            "shipped_at": r.shipped_at.isoformat() if r.shipped_at else None,
            "shipped_date_local": r.shipped_date_local.isoformat() if r.shipped_date_local else None,
            "memo": r.memo,
            "printed_at": r.printed_at.isoformat() if r.printed_at else None,
            "billing_address_line1": r.billing_address_line1,
            "billing_address_line2": r.billing_address_line2,
            "billing_address_city": r.billing_address_city,
            "billing_address_state": r.billing_address_state,
            "billing_address_postal_code": r.billing_address_postal_code,
            "shipping_address_line1": r.shipping_address_line1,
            "shipping_address_line2": r.shipping_address_line2,
            "shipping_address_city": r.shipping_address_city,
            "shipping_address_state": r.shipping_address_state,
            "shipping_address_postal_code": r.shipping_address_postal_code,
        }
        if include_primary_bin:
            out["primary_bin_code"] = r.primary_bin_code
            out["primary_bin_pick_sequence"] = r.primary_bin_pick_sequence
        return out

    return jsonify({
        "sales_orders": [_row_to_dict(r) for r in rows],
        "total": total, "page": page, "per_page": per_page, "pages": pages,
    })


@admin_bp.route("/sales-orders/<int:so_id>", methods=["GET"])
@require_auth
@require_admin_or_page_permission("sales-orders")
@with_db
def get_sales_order(so_id):
    so = g.db.execute(
        text("""
            SELECT so_id, so_number, so_barcode,
                   customer_name, customer_phone, customer_address,
                   status, priority,
                   warehouse_id, ship_method, ship_address,
                   order_date, ship_by_date, created_at, picked_at, packed_at,
                   shipped_at, created_by,
                   (shipped_at AT TIME ZONE :company_tz)::date AS shipped_date_local,
                   order_total, customer_shipping_paid, memo,
                   source_system,
                   order_origin,
                   carrier, tracking_number,
                   order_type, parent_so_id,
                   backorder_opened_at, backorder_fulfillable_at,
                   cancellation_reason,
                   billing_address_name, billing_address_line1, billing_address_line2,
                   billing_address_city, billing_address_state,
                   billing_address_postal_code, billing_address_country,
                   billing_address_phone,
                   shipping_address_name, shipping_address_line1, shipping_address_line2,
                   shipping_address_city, shipping_address_state,
                   shipping_address_postal_code, shipping_address_country,
                   shipping_address_phone
              FROM sales_orders WHERE so_id = :sid
        """),
        {"sid": so_id, "company_tz": COMPANY_TIMEZONE},
    ).fetchone()
    if not so:
        return jsonify({"error": "Sales order not found"}), 404

    lines = g.db.execute(
        text("""
            SELECT sol.so_line_id, sol.line_number, sol.item_id, i.sku, i.item_name, i.upc,
                   sol.quantity_ordered, sol.quantity_allocated, sol.quantity_picked, sol.quantity_packed, sol.quantity_shipped, sol.status
            FROM sales_order_lines sol JOIN items i ON i.item_id = sol.item_id
            WHERE sol.so_id = :sid ORDER BY sol.line_number
        """),
        {"sid": so_id},
    ).fetchall()

    # so-refinement: pick_tasks still in PICKED state. The revert-status
    # modal needs item / bin attribution to offer per-pick release; the
    # detail GET is the natural place to return it so the modal does
    # not need a second round-trip when the operator demotes status.
    pick_tasks = g.db.execute(
        text("""
            SELECT pt.pick_task_id, pt.so_line_id, pt.item_id, pt.bin_id,
                   pt.quantity_picked, pt.picked_at, pt.picked_by,
                   pt.status,
                   i.sku, i.item_name, b.bin_code
              FROM pick_tasks pt
              JOIN items i ON i.item_id = pt.item_id
              JOIN bins b ON b.bin_id = pt.bin_id
             WHERE pt.so_id = :sid AND pt.status = 'PICKED'
             ORDER BY pt.pick_task_id
        """),
        {"sid": so_id},
    ).fetchall()

    return jsonify({
        "sales_order": {
            "so_id": so.so_id, "so_number": so.so_number, "so_barcode": so.so_barcode,
            "customer_name": so.customer_name,
            # customer_phone + customer_address were previously missing
            # from this response, so the edit modal reloaded with the
            # phone field blank after a save and operators assumed
            # the save had failed. Both fields are returned now so
            # round-trip edits display the new value.
            "customer_phone": so.customer_phone,
            "customer_address": so.customer_address,
            "status": so.status, "priority": so.priority,
            "warehouse_id": so.warehouse_id, "ship_method": so.ship_method, "ship_address": so.ship_address,
            "order_date": so.order_date.isoformat() if so.order_date else None,
            "ship_by_date": so.ship_by_date.isoformat() if so.ship_by_date else None,
            "created_at": so.created_at.isoformat() if so.created_at else None,
            "created_by": so.created_by,
            # v1.8.0 (#282): per-order cost fields. Null vs 0.00 is
            # distinct (not provided vs explicitly zero). Decimal
            # serialised as string so the JSON does not lose precision.
            "order_total": str(so.order_total) if so.order_total is not None else None,
            "customer_shipping_paid": (
                str(so.customer_shipping_paid)
                if so.customer_shipping_paid is not None else None
            ),
            # v1.9.0: free-text operator-facing note (mig 055).
            "memo": so.memo,
            # Shipped date as the company-local calendar date (UTC
            # shipped_at converted via COMPANY_TIMEZONE in SQL). The admin
            # modal displays and edits this single value so the read-only
            # view and the date picker can never disagree.
            "shipped_date_local": (
                so.shipped_date_local.isoformat() if so.shipped_date_local else None
            ),
            # source_system: denormalised tag (mig 062). NULL for
            # admin-created and POS-created SOs.
            "source_system": so.source_system,
            # mig 063: free-text upstream-origin label populated by the
            # inbound payload mapping. NULL when the connector has not
            # been wired to provide it.
            "order_origin": so.order_origin,
            # so-refinement: shipment-state fields. Populated by dockd
            # ship POST; surfaced here so the edit modal can show the
            # current tracking number and the view modal can display it.
            "carrier": so.carrier,
            "tracking_number": so.tracking_number,
            # Backorder lifecycle (mig 067). The admin Edit modal uses
            # order_type + parent_so_id to gate the "Partially Fulfill"
            # button (rejects backorders to enforce the one-level
            # chaining cap). backorder_*_at + cancellation_reason are
            # read-only context for the Backorders page detail view.
            "order_type":                so.order_type,
            "parent_so_id":              so.parent_so_id,
            "backorder_opened_at":       so.backorder_opened_at.isoformat() if so.backorder_opened_at else None,
            "backorder_fulfillable_at":  so.backorder_fulfillable_at.isoformat() if so.backorder_fulfillable_at else None,
            "cancellation_reason":       so.cancellation_reason,
            # v1.8.0 (#288): structured billing/shipping address fields.
            **{name: getattr(so, name) for name in ADDRESS_FIELD_NAMES},
        },
        "lines": [
            {"so_line_id": l.so_line_id, "line_number": l.line_number, "item_id": l.item_id,
             "sku": l.sku, "item_name": l.item_name, "upc": l.upc,
             "quantity_ordered": l.quantity_ordered, "quantity_allocated": l.quantity_allocated,
             "quantity_picked": l.quantity_picked, "quantity_packed": l.quantity_packed,
             "quantity_shipped": l.quantity_shipped, "status": l.status}
            for l in lines
        ],
        "pick_tasks": [
            {"pick_task_id": p.pick_task_id, "so_line_id": p.so_line_id,
             "item_id": p.item_id, "sku": p.sku, "item_name": p.item_name,
             "bin_id": p.bin_id, "bin_code": p.bin_code,
             "quantity_picked": p.quantity_picked,
             "picked_at": p.picked_at.isoformat() if p.picked_at else None,
             "picked_by": p.picked_by,
             "status": p.status}
            for p in pick_tasks
        ],
    })


def _picking_ticket_branding(db):
    """Configurable packing-slip branding from app_settings. All four keys
    default to empty so a fresh install renders a clean, unbranded slip; an
    operator sets them under Settings > Picking Ticket."""
    rows = db.execute(
        text(
            "SELECT key, value FROM app_settings WHERE key IN ("
            "'picking_ticket_company_name', 'picking_ticket_company_address', "
            "'picking_ticket_logo_url', 'picking_ticket_returns_text')"
        )
    ).fetchall()
    vals = {r.key: r.value for r in rows}
    return {
        "company_name": vals.get("picking_ticket_company_name", ""),
        "company_address": vals.get("picking_ticket_company_address", ""),
        "logo_url": vals.get("picking_ticket_logo_url", ""),
        "returns_text": vals.get("picking_ticket_returns_text", ""),
    }


@admin_bp.route("/sales-orders/<int:so_id>/picking-ticket", methods=["GET"])
@require_auth
@require_admin_or_page_permission("picking-tickets")
@with_db
def get_picking_ticket(so_id):
    """Picking-ticket view of an SO: header + lines with the top-priority
    preferred bin for each item, scoped to the SO's warehouse. Used by
    the Outbound > Picking Tickets print page."""
    so = g.db.execute(
        text("""
            SELECT so_id, so_number, customer_name, status,
                   warehouse_id, ship_method,
                   order_date, ship_by_date, created_at,
                   shipping_address_name, shipping_address_line1, shipping_address_line2,
                   shipping_address_city, shipping_address_state,
                   shipping_address_postal_code, shipping_address_country
              FROM sales_orders WHERE so_id = :sid
        """),
        {"sid": so_id},
    ).fetchone()
    if not so:
        return jsonify({"error": "Sales order not found"}), 404

    # Per line, fetch the top-priority preferred bin scoped to the SO's
    # warehouse. A LEFT JOIN LATERAL keeps the row when there is no
    # preferred bin so the picker still sees the SKU with a blank bin
    # column rather than the line dropping out entirely.
    lines = g.db.execute(
        text("""
            SELECT sol.so_line_id, sol.line_number, sol.item_id,
                   i.sku, i.item_name, i.upc,
                   sol.quantity_ordered, sol.quantity_shipped,
                   pref.bin_code AS preferred_bin_code
            FROM sales_order_lines sol
            JOIN items i ON i.item_id = sol.item_id
            LEFT JOIN LATERAL (
                SELECT b.bin_code
                  FROM preferred_bins pb
                  JOIN bins b ON b.bin_id = pb.bin_id
                 WHERE pb.item_id = sol.item_id
                   AND b.warehouse_id = :wid
                 ORDER BY pb.priority ASC
                 LIMIT 1
            ) pref ON TRUE
            WHERE sol.so_id = :sid
            ORDER BY sol.line_number
        """),
        {"sid": so_id, "wid": so.warehouse_id},
    ).fetchall()

    return jsonify({
        "sales_order": {
            "so_id": so.so_id, "so_number": so.so_number,
            "customer_name": so.customer_name, "status": so.status,
            "warehouse_id": so.warehouse_id, "ship_method": so.ship_method,
            "order_date": so.order_date.isoformat() if so.order_date else None,
            "ship_by_date": so.ship_by_date.isoformat() if so.ship_by_date else None,
            "created_at": so.created_at.isoformat() if so.created_at else None,
            "shipping_address_name": so.shipping_address_name,
            "shipping_address_line1": so.shipping_address_line1,
            "shipping_address_line2": so.shipping_address_line2,
            "shipping_address_city": so.shipping_address_city,
            "shipping_address_state": so.shipping_address_state,
            "shipping_address_postal_code": so.shipping_address_postal_code,
            "shipping_address_country": so.shipping_address_country,
        },
        "lines": [
            {"so_line_id": l.so_line_id, "line_number": l.line_number,
             "sku": l.sku, "item_name": l.item_name, "upc": l.upc,
             "quantity_ordered": l.quantity_ordered,
             "quantity_shipped": l.quantity_shipped,
             "preferred_bin_code": l.preferred_bin_code}
            for l in lines
        ],
        "branding": _picking_ticket_branding(g.db),
    })


@admin_bp.route("/sales-orders/mark-printed", methods=["POST"])
@require_auth
@require_admin_or_page_permission("picking-tickets")
@validate_body(MarkSalesOrdersPrintedRequest)
@with_db
def mark_sales_orders_printed(validated):
    """Stamp printed_at = NOW() for a batch of SOs whose picking
    tickets the client has successfully rendered. The picking ticket
    print pages POST here so the Picking Tickets queue can hide
    already-printed orders without coupling render success to a
    server-side GET side effect."""
    so_ids = validated.so_ids
    rows = g.db.execute(
        text(
            """
            UPDATE sales_orders
               SET printed_at = NOW()
             WHERE so_id = ANY(:ids)
               AND printed_at IS NULL
            RETURNING so_id, printed_at
            """
        ),
        {"ids": so_ids},
    ).fetchall()
    g.db.commit()
    return jsonify({
        "marked": [
            {"so_id": r.so_id, "printed_at": r.printed_at.isoformat()}
            for r in rows
        ],
    })


@admin_bp.route("/sales-orders", methods=["POST"])
@require_auth
@require_admin_or_page_permission("sales-orders")
@validate_body(CreateSalesOrderRequest)
@with_db
def create_sales_order(validated):
    data = validated.model_dump()

    dup = g.db.execute(text("SELECT 1 FROM sales_orders WHERE so_number = :sn"), {"sn": data["so_number"]}).fetchone()
    if dup:
        return jsonify({"error": f"Duplicate so_number: {data['so_number']}"}), 400

    # Validate items exist in DB
    for line in data["lines"]:
        item = g.db.execute(text("SELECT 1 FROM items WHERE item_id = :iid"), {"iid": line["item_id"]}).fetchone()
        if not item:
            return jsonify({"error": f"Item {line['item_id']} not found"}), 400

    result = g.db.execute(
        text("""
            INSERT INTO sales_orders (so_number, so_barcode, customer_name, customer_phone, customer_address, warehouse_id, ship_method, ship_address, ship_by_date, memo, order_origin, order_date, created_by, status, external_id)
            VALUES (:sn, :sb, :cust, :phone, :caddr, :wid, :ship, :addr, :ship_by, :memo, :origin, NOW(), :created_by, :status, :ext_id)
            RETURNING so_id
        """),
        {
            "sn": data["so_number"], "sb": data.get("so_barcode", data["so_number"]),
            "cust": data.get("customer_name"), "phone": data.get("customer_phone"),
            "caddr": data.get("customer_address"),
            "wid": data["warehouse_id"],
            "ship": data.get("ship_method"), "addr": data.get("ship_address"),
            "ship_by": data.get("ship_by_date"), "memo": data.get("memo"),
            "origin": data.get("order_origin"),
            "created_by": g.current_user["username"],
            "status": SO_OPEN,
            "ext_id": str(uuid.uuid4()),
        },
    )
    so_id = result.fetchone()[0]

    for line in data["lines"]:
        g.db.execute(
            text("INSERT INTO sales_order_lines (so_id, item_id, quantity_ordered, line_number) VALUES (:sid, :iid, :qty, :ln)"),
            {"sid": so_id, "iid": line["item_id"], "qty": line["quantity_ordered"], "ln": line.get("line_number") or 1},
        )

    g.db.commit()

    # Re-fetch to return (save/restore g.db since get_sales_order has @with_db)
    outer_db = g.db
    response = get_sales_order(so_id)
    g.db = outer_db
    return response


@admin_bp.route("/sales-orders/<int:so_id>", methods=["PUT"])
@require_auth
@require_admin_or_page_permission("sales-orders")
@validate_body(UpdateSalesOrderRequest)
@with_db
def update_sales_order(so_id, validated):
    """Admin edit of SO non-line fields.

    Status policy:
      * ADMIN: edit at any status (including SHIPPED, for backfill of
        orders shipped through external systems).
      * USER with the so-full-edit override: edit at any status except
        SHIPPED.
      * USER without the override: edit only when status = OPEN.

    Source-system reassignment (mig 062) is gated to ADMIN or
    so-full-edit because it changes which ERP a downstream replay
    would target. The 16 address fields stay on /address PATCH so the
    address-only edit path keeps its own audit shape.

    Per-field audit rows mirror the address surface: one row per
    actually-changed field carrying old/new so a post-edit forensic
    diff does not require scanning row state.
    """
    data = validated.model_dump(exclude_unset=True)

    so = g.db.execute(
        text(
            "SELECT so_id, status, warehouse_id, source_system, order_origin, "
            "       so_number, so_barcode, "
            "       customer_name, customer_phone, customer_address, "
            "       ship_method, ship_address, ship_by_date, "
            "       priority, memo, "
            "       status AS cur_status, carrier, tracking_number, shipped_at "
            "  FROM sales_orders WHERE so_id = :sid FOR UPDATE"
        ),
        {"sid": so_id},
    ).fetchone()
    if not so:
        return jsonify({"error": "Sales order not found"}), 404

    role = g.current_user.get("role")
    is_admin = role == ROLE_ADMIN
    has_so_override = has_override(OVERRIDE_SO_FULL_EDIT)

    if not is_admin:
        # Non-admin status gate. Without override: only OPEN. With
        # override: any non-SHIPPED status.
        if has_so_override:
            if so.status == SO_SHIPPED:
                return jsonify({
                    "error": "Cannot edit a SHIPPED order; void the shipment first",
                    "current_status": so.status,
                }), 403
        elif so.status != SO_OPEN:
            return jsonify({
                "error": "non-admin can only edit OPEN sales orders without the so-full-edit override",
                "current_status": so.status,
            }), 403

    # source_system reassignment is the riskier knob: ADMIN-or-override.
    # If the field was sent by a caller lacking authority, reject before
    # the UPDATE so partial writes never land.
    if "source_system" in data and not (is_admin or has_so_override):
        return jsonify({
            "error": "source_system reassignment requires ADMIN or so-full-edit override",
        }), 403

    ALLOWED_FIELDS = {
        "so_number", "so_barcode",
        "customer_name", "customer_phone", "customer_address",
        "ship_method", "ship_address", "ship_by_date",
        "priority", "memo", "source_system",
        # Shipment-state edits for backfill of orders shipped via
        # external systems (e.g. a retiring shipping bridge) so the warehouse view
        # reflects the right status without going through the dockd
        # PICKED -> PACKED -> SHIPPED chain. Direct UPDATE -- does NOT
        # write inventory_movements or outbox events; for one-off data
        # corrections only. Routine fulfillment still flows through
        # /api/v1/dockd/orders/<so_number>/ship.
        "status", "carrier", "tracking_number", "shipped_at",
        # mig 063: free-text upstream-origin label.
        "order_origin",
    }
    fields, params, edits = [], {"sid": so_id}, []
    for col in ALLOWED_FIELDS:
        if col not in data:
            continue
        # shipped_at is handled separately below: it is a calendar date
        # anchored at noon in the company timezone, not a raw passthrough.
        if col == "shipped_at":
            continue
        new_value = data[col]
        # source_system empty string -> NULL so a CSR can clear an
        # ERP mis-tag without picking a placeholder system. Other
        # text fields keep their existing semantics.
        if col == "source_system" and new_value == "":
            new_value = None
        old_value = getattr(so, col)
        if old_value == new_value:
            continue
        fields.append(f"{col} = :{col}")
        params[col] = new_value
        edits.append((col, old_value, new_value))

    # shipped_at: operator-entered Shipped Date. The wire value is a
    # calendar date (YYYY-MM-DD); anchor it at noon in COMPANY_TIMEZONE
    # so the stored UTC instant always reads back as that same date
    # regardless of DST. Empty string clears to NULL. dockd ship writes
    # the precise NOW() instant directly and is unaffected. Change
    # detection compares company-local dates so a no-op re-save writes no
    # audit row.
    if "shipped_at" in data:
        raw = (data["shipped_at"] or "").strip()
        old_local = (
            so.shipped_at.astimezone(ZoneInfo(COMPANY_TIMEZONE)).date().isoformat()
            if so.shipped_at else ""
        )
        if raw != old_local:
            if raw:
                # CAST(... AS date), not :shipped_date::date -- SQLAlchemy
                # text() mis-parses a :param immediately followed by the ::
                # cast operator and leaves the bind unsubstituted.
                fields.append(
                    "shipped_at = (CAST(:shipped_date AS date) + time '12:00') "
                    "AT TIME ZONE :company_tz"
                )
                params["shipped_date"] = raw
                params["company_tz"] = COMPANY_TIMEZONE
            else:
                fields.append("shipped_at = NULL")
            edits.append(("shipped_at", so.shipped_at, raw or None))

    if not fields:
        return jsonify({"unchanged": True}), 200

    g.db.execute(
        text(f"UPDATE sales_orders SET {', '.join(fields)} WHERE so_id = :sid"),
        params,
    )

    user_id = g.current_user["user_id"]
    for field_changed, old_value, new_value in edits:
        action = (
            ACTION_SO_SOURCE_SYSTEM_REASSIGNED
            if field_changed == "source_system"
            else ACTION_SO_HEADER_EDITED
        )
        write_audit_log(
            g.db,
            action_type=action,
            entity_type="SO",
            entity_id=so_id,
            user_id=user_id,
            warehouse_id=so.warehouse_id,
            details={
                "field_changed": field_changed,
                "old_value": str(old_value) if old_value is not None else None,
                "new_value": str(new_value) if new_value is not None else None,
            },
        )

    g.db.commit()

    row = g.db.execute(
        text(
            "SELECT so_id, so_number, so_barcode, customer_name, status, "
            "       warehouse_id, ship_method, ship_address, source_system, "
            "       created_at "
            "  FROM sales_orders WHERE so_id = :sid"
        ),
        {"sid": so_id},
    ).fetchone()
    return jsonify({
        "so_id": row.so_id, "so_number": row.so_number, "so_barcode": row.so_barcode,
        "customer_name": row.customer_name, "status": row.status,
        "warehouse_id": row.warehouse_id, "ship_method": row.ship_method,
        "ship_address": row.ship_address, "source_system": row.source_system,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "edited_fields": [e[0] for e in edits],
    })


@admin_bp.route("/sales-orders/<int:so_id>/address", methods=["PATCH"])
@require_auth
@validate_body(UpdateSalesOrderAddressRequest)
@with_db
def update_sales_order_address(so_id, validated):
    """v1.8.0 (#288): edit the 16 structured billing/shipping address
    fields on a sales_order. Status gate: ADMIN can edit at any
    status; non-admin only at status='OPEN'. One audit row per
    actually-changed field carrying {field_changed, old_value,
    new_value} so investigators can reconstruct the diff without
    scanning the 16-column row state.
    """
    role = g.current_user.get("role")

    so = g.db.execute(
        text(f"""
            SELECT so_id, status, warehouse_id,
                   {", ".join(ADDRESS_FIELD_NAMES)}
              FROM sales_orders WHERE so_id = :sid FOR UPDATE
        """),
        {"sid": so_id},
    ).fetchone()
    if not so:
        return jsonify({"error": "Sales order not found"}), 404
    if role != ROLE_ADMIN and so.status != SO_OPEN:
        return jsonify({
            "error": "non-admin can only edit address on OPEN sales orders",
            "current_status": so.status,
        }), 403

    data = validated.model_dump(exclude_unset=True)
    if not data:
        return jsonify({"error": "no address fields provided"}), 400

    fields, params, edits = [], {"sid": so_id}, []
    for col, new_value in data.items():
        old_value = getattr(so, col)
        # Treat empty string as explicit clear -> NULL.
        normalized_new = new_value if new_value != "" else None
        if old_value == normalized_new:
            continue
        fields.append(f"{col} = :{col}")
        params[col] = normalized_new
        edits.append((col, old_value, normalized_new))

    if not fields:
        return jsonify({"unchanged": True}), 200

    g.db.execute(
        text(f"UPDATE sales_orders SET {', '.join(fields)} WHERE so_id = :sid"),
        params,
    )

    for field_changed, old_value, new_value in edits:
        write_audit_log(
            g.db,
            action_type=ACTION_SO_ADDRESS_EDITED,
            entity_type="SO",
            entity_id=so_id,
            user_id=g.current_user["username"],
            warehouse_id=so.warehouse_id,
            details={
                "field_changed": field_changed,
                "old_value": old_value,
                "new_value": new_value,
            },
        )

    g.db.commit()
    return jsonify({
        "so_id": so_id,
        "edited_fields": [e[0] for e in edits],
    })


@admin_bp.route("/sales-orders/<int:so_id>/memo", methods=["PATCH"])
@require_auth
@require_admin_or_page_permission("fraud")
@validate_body(UpdateSalesOrderMemoRequest)
@with_db
def update_sales_order_memo(so_id, validated):
    """Edit the free-form CSR memo on an SO (Outbound > Fraud page).
    Empty string clears to NULL. One audit row per actual change
    carrying old/new for a forensic diff."""
    so = g.db.execute(
        text("SELECT so_id, memo, warehouse_id FROM sales_orders WHERE so_id = :sid FOR UPDATE"),
        {"sid": so_id},
    ).fetchone()
    if not so:
        return jsonify({"error": "Sales order not found"}), 404

    new_memo = validated.memo if validated.memo != "" else None
    if so.memo == new_memo:
        return jsonify({"unchanged": True, "memo": so.memo}), 200

    g.db.execute(
        text("UPDATE sales_orders SET memo = :memo WHERE so_id = :sid"),
        {"memo": new_memo, "sid": so_id},
    )
    write_audit_log(
        g.db,
        action_type=ACTION_SO_MEMO_EDITED,
        entity_type="SO",
        entity_id=so_id,
        user_id=g.current_user["username"],
        warehouse_id=so.warehouse_id,
        details={"old_value": so.memo, "new_value": new_memo},
    )
    g.db.commit()
    return jsonify({"so_id": so_id, "memo": new_memo})


@admin_bp.route("/sales-orders/<int:so_id>/push-to-queue", methods=["POST"])
@require_auth
@require_admin_or_page_permission("fraud")
@with_db
def push_sales_order_to_queue(so_id):
    """Clear an auto-flagged fraud SO and return it to the picking
    queue. Only valid when status=FRAUD_REVIEW; any other current
    status is a 409 because this path does not know how to transition
    out of (e.g.) PICKING. Status moves to OPEN."""
    so = g.db.execute(
        text("SELECT so_id, status, warehouse_id FROM sales_orders WHERE so_id = :sid FOR UPDATE"),
        {"sid": so_id},
    ).fetchone()
    if not so:
        return jsonify({"error": "Sales order not found"}), 404
    if so.status != SO_FRAUD_REVIEW:
        return jsonify({
            "error": f"Can only push FRAUD_REVIEW orders to the queue. Current: {so.status}",
            "current_status": so.status,
        }), 409

    g.db.execute(
        text("UPDATE sales_orders SET status = :status WHERE so_id = :sid"),
        {"status": SO_OPEN, "sid": so_id},
    )
    write_audit_log(
        g.db,
        action_type=ACTION_SO_FRAUD_CLEARED,
        entity_type="SO",
        entity_id=so_id,
        user_id=g.current_user["username"],
        warehouse_id=so.warehouse_id,
        details={"from_status": SO_FRAUD_REVIEW, "to_status": SO_OPEN},
    )
    g.db.commit()
    return jsonify({"so_id": so_id, "status": SO_OPEN})


@admin_bp.route("/sales-orders/<int:so_id>/cancel", methods=["POST"])
@require_auth
@require_admin_or_page_permission("sales-orders")
@with_db
def cancel_sales_order(so_id):
    """Operator-initiated cancel. Delegates to the shared
    sales_order_service.cancel_sales_order so audit-log writing,
    per-status unwind, and SHIPPED rejection match the inbound path."""
    username = g.current_user["username"]
    try:
        result = _cancel_so(
            g.db, so_id=so_id, source="admin", username=username,
        )
    except CancelNotAllowed as exc:
        if exc.current_status == "UNKNOWN":
            return jsonify({"error": "Sales order not found"}), 404
        return jsonify({
            "error": str(exc),
            "current_status": exc.current_status,
        }), 400
    g.db.commit()
    return jsonify({
        "message": "Sales order cancelled",
        "pre_status": result["pre_status"],
        "audit_log_id": result["audit_log_id"],
    })


@admin_bp.route("/sales-orders/<int:so_id>/revert-status", methods=["POST"])
@require_auth
@require_admin_or_page_permission("sales-orders")
@validate_body(RevertSalesOrderStatusRequest)
@with_db
def revert_sales_order_status(so_id, validated):
    """so-refinement: admin demotes an SO from PICKED/PACKED/SHIPPED
    back to an earlier status. Delegates to the shared service so the
    pick-task release, unpack, and unship effects share one transaction
    and one audit shape. RevertNotAllowed.kind discriminates the 4xx
    response so the frontend can route the error to the right UI."""
    try:
        result = _revert_so_status(
            g.db,
            so_id=so_id,
            new_status=validated.new_status,
            release_pick_task_ids=validated.release_pick_task_ids,
            username=g.current_user["username"],
        )
    except RevertNotAllowed as exc:
        status_code = 404 if exc.kind == "not_found" else (
            409 if exc.kind == "picked_qty_remaining" else 400
        )
        body = {"error": str(exc), "kind": exc.kind, **exc.context}
        return jsonify(body), status_code
    g.db.commit()
    return jsonify({
        "message": "Sales order status reverted",
        **result,
    })


# Admin virtual pick: bin availability lookup for the admin-pick modal.
#
# Powers the per-line bin dropdown. Returns bins in the SO's warehouse
# that hold the line's item with available stock (on_hand - allocated
# > 0), sorted by preferred-bin priority first then bin_code. The
# admin-pick POST also validates warehouse + availability on submit,
# so a stale dropdown read doesn't corrupt the pick -- this endpoint
# is purely for UX.


@admin_bp.route(
    "/sales-orders/<int:so_id>/lines/<int:so_line_id>/available-bins",
    methods=["GET"],
)
@require_auth
@require_admin_or_page_permission("sales-orders")
@with_db
def admin_pick_available_bins(so_id, so_line_id):
    """List bins where the SO line's item has available stock.

    Scope: bins in the same warehouse as the SO. Filter:
    quantity_on_hand - quantity_allocated > 0. Order: preferred-bin
    priority ascending (NULL LAST so non-preferred bins still appear),
    then bin_code ascending for a stable tiebreaker.
    """
    so = g.db.execute(
        text(
            "SELECT s.warehouse_id, l.item_id "
            "  FROM sales_orders s "
            "  JOIN sales_order_lines l ON l.so_id = s.so_id "
            " WHERE s.so_id = :sid AND l.so_line_id = :lid"
        ),
        {"sid": so_id, "lid": so_line_id},
    ).fetchone()
    if so is None:
        return jsonify({
            "error": "sales order line not found or not on this SO",
        }), 404

    rows = g.db.execute(
        text(
            """
            SELECT b.bin_id, b.bin_code, z.zone_name,
                   inv.quantity_on_hand, inv.quantity_allocated,
                   (inv.quantity_on_hand - inv.quantity_allocated)
                       AS quantity_available,
                   pb.priority AS preferred_priority
              FROM inventory inv
              JOIN bins b   ON b.bin_id = inv.bin_id
              JOIN zones z  ON z.zone_id = b.zone_id
              LEFT JOIN preferred_bins pb
                ON pb.item_id = inv.item_id AND pb.bin_id = inv.bin_id
             WHERE inv.item_id = :iid
               AND b.warehouse_id = :wh
               AND inv.quantity_on_hand - inv.quantity_allocated > 0
             ORDER BY pb.priority NULLS LAST, b.bin_code ASC
            """
        ),
        {"iid": so.item_id, "wh": so.warehouse_id},
    ).fetchall()

    return jsonify({
        "warehouse_id": so.warehouse_id,
        "item_id": so.item_id,
        "bins": [
            {
                "bin_id": r.bin_id,
                "bin_code": r.bin_code,
                "zone_name": r.zone_name,
                "quantity_on_hand": r.quantity_on_hand,
                "quantity_allocated": r.quantity_allocated,
                "quantity_available": r.quantity_available,
                "preferred_priority": r.preferred_priority,
            }
            for r in rows
        ],
    })


# Admin virtual pick: operator-driven OPEN -> PICKED via real picks.
#
# The use case is the SO got picked physically but the digital pick never
# landed (legacy bridge, stuck batch, fulfilled-on-arrival inbound). The
# normal ship guard (quantity_picked > 0 per line) blocks the manual
# OPEN -> SHIPPED flip until this fires. End state is indistinguishable
# from a handheld pick: line counters bump, inventory decrements, audit
# row per pick, pick.confirmed emits when the SO completes. Undo lives
# in the existing release-pick-tasks UI -- the synthetic pick_tasks
# created here slot in like real ones.


@admin_bp.route("/sales-orders/<int:so_id>/admin-pick", methods=["POST"])
@require_auth
@require_admin_or_page_permission("sales-orders")
@validate_body(AdminPickRequest)
@with_db
def admin_pick_sales_order(so_id, validated):
    """Apply a batched admin virtual pick to an OPEN sales order.

    Body shape: {lines: [{so_line_id, bin_id, quantity}, ...]}. Multiple
    entries with the same so_line_id are allowed (split-bin). The
    service layer is all-or-nothing in one transaction; any failure
    rolls back every line in the batch.

    Auth: ADMIN role OR so-full-edit override. The base sales-orders
    page permission gates entry; the stricter check is here because
    admin-pick mutates inventory.
    """
    role = g.current_user.get("role")
    if role != ROLE_ADMIN and not has_override(OVERRIDE_SO_FULL_EDIT):
        return jsonify({
            "error": "admin-pick requires ADMIN or so-full-edit override",
        }), 403

    try:
        result = record_admin_pick(
            g.db,
            so_id=so_id,
            picks=[ln.model_dump() for ln in validated.lines],
            username=g.current_user["username"],
        )
        promoted = maybe_promote_so_to_picked(
            g.db,
            so_id=so_id,
            username=g.current_user["username"],
        )
    except AdminPickError as exc:
        status_code = 409 if exc.kind == "insufficient_available" else 422
        return jsonify({
            "error": str(exc),
            "kind": exc.kind,
            **exc.context,
        }), status_code

    g.db.commit()
    return jsonify({
        "message": "Admin pick applied",
        "promoted_to_picked": bool(promoted),
        **result,
    })


# ── Partial Fulfill / Backorders ─────────────────────────────────────────────
#
# Partial-fulfill shrinks one or more lines on an existing SO and
# creates a new backorder SO (status=WAITING_STOCK) carrying the
# shorted qty. The original SO continues through pack -> ship with
# what was actually picked; the BO waits for the receiving hook in
# receiving.py to flip it to OPEN when matching stock arrives.
#
# Status gate: {OPEN, PICKED}. OPEN is allowed for any sales-orders
# user; PICKED requires ADMIN or the so-full-edit override (same
# escalation as the post-OPEN line edit gate at _so_line_edit_gate).
# Validation rule `short_qty <= quantity_ordered - quantity_picked`
# keeps PICKED-shorting safe: it is only valid when the SO has
# unpicked qty (short-picks happened during the batch). The
# defective-tote case (PICKED-with-everything-picked) requires the
# operator to cancel and recreate; out of scope for v1.
#
# BO chaining is capped at one level: a SO with parent_so_id IS NOT
# NULL rejects partial-fulfill (the UI hides the button too, but the
# server is authoritative).

ADDRESS_COPY_FIELDS = (
    "billing_address_name", "billing_address_line1", "billing_address_line2",
    "billing_address_city", "billing_address_state",
    "billing_address_postal_code", "billing_address_country",
    "billing_address_phone",
    "shipping_address_name", "shipping_address_line1", "shipping_address_line2",
    "shipping_address_city", "shipping_address_state",
    "shipping_address_postal_code", "shipping_address_country",
    "shipping_address_phone",
)


def _select_so_for_partial_fulfill(db, so_id):
    """Returns the SO row with every column the BO clone needs.

    Locked FOR UPDATE so a concurrent /cancel or /update cannot
    transition past us mid-shrink. Centralised so the partial-fulfill
    handler does not carry a 30-column SELECT inline."""
    cols = (
        "so_id, so_number, external_id, status, warehouse_id, parent_so_id, "
        "order_type, customer_name, customer_id, customer_phone, "
        "customer_address, ship_method, ship_address, source_system, memo, "
        "created_by, "
        + ", ".join(ADDRESS_COPY_FIELDS)
    )
    return db.execute(
        text(f"SELECT {cols} FROM sales_orders WHERE so_id = :sid FOR UPDATE"),
        {"sid": so_id},
    ).fetchone()


def _write_short_adjustment(db, *, item_id, warehouse_id, short_qty, reason_detail, bo_so_number, username):
    """If there is on-hand stock for this item in this warehouse, write
    a SHORT inventory_adjustments row and decrement on-hand from the bin
    with the most available units.

    Returns the adjusted qty (0 if no on-hand stock existed). The bin
    pick is deterministic (most-available, then lowest bin_id) so
    tests can assert against it."""
    bin_row = db.execute(
        text(
            "SELECT bin_id, quantity_on_hand, quantity_allocated "
            "  FROM inventory "
            " WHERE item_id = :iid AND warehouse_id = :wid "
            "   AND (quantity_on_hand - quantity_allocated) > 0 "
            " ORDER BY (quantity_on_hand - quantity_allocated) DESC, bin_id ASC "
            " LIMIT 1"
        ),
        {"iid": item_id, "wid": warehouse_id},
    ).fetchone()
    if not bin_row:
        return 0
    available = bin_row.quantity_on_hand - bin_row.quantity_allocated
    adj_qty = min(available, short_qty)
    if adj_qty <= 0:
        return 0
    db.execute(
        text(
            "UPDATE inventory "
            "   SET quantity_on_hand = GREATEST(0, quantity_on_hand - :qty), "
            "       updated_at = NOW() "
            " WHERE item_id = :iid AND bin_id = :bid"
        ),
        {"qty": adj_qty, "iid": item_id, "bid": bin_row.bin_id},
    )
    db.execute(
        text(
            "INSERT INTO inventory_adjustments ("
            "  item_id, bin_id, warehouse_id, quantity_change, reason_code, "
            "  reason_detail, status, adjusted_by, adjusted_at, external_id"
            ") VALUES ("
            "  :iid, :bid, :wid, :qty_change, :reason_code, :reason_detail, "
            "  :status, :user, NOW(), :ext_id"
            ")"
        ),
        {
            "iid": item_id,
            "bid": bin_row.bin_id,
            "wid": warehouse_id,
            "qty_change": -adj_qty,
            "reason_code": ADJ_REASON_SHORT,
            "reason_detail": f"{reason_detail} (BO: {bo_so_number})",
            "status": ADJ_APPROVED,
            "user": username,
            "ext_id": str(uuid.uuid4()),
        },
    )
    return adj_qty


@admin_bp.route("/sales-orders/<int:so_id>/partial-fulfill", methods=["POST"])
@require_auth
@require_admin_or_page_permission("sales-orders")
@validate_body(PartialFulfillRequest)
@with_db
def partial_fulfill_sales_order(so_id, validated):
    """Ship what's pickable, create a backorder SO for the rest."""
    so = _select_so_for_partial_fulfill(g.db, so_id)
    if not so:
        return jsonify({"error": "Sales order not found"}), 404

    if so.status not in (SO_OPEN, SO_PICKED):
        return jsonify({
            "error": f"Can only partial-fulfill OPEN or PICKED orders. Current: {so.status}",
            "current_status": so.status,
        }), 400

    # BO chaining cap: a backorder cannot itself spawn a backorder.
    if so.parent_so_id is not None:
        return jsonify({
            "error": "This SO is a backorder; cancel it and create a fresh standalone SO instead of chaining backorders.",
            "parent_so_id": so.parent_so_id,
        }), 400

    if so.order_type == "refund":
        return jsonify({"error": "Cannot partial-fulfill a refund SO."}), 400

    # PICKED requires ADMIN or so-full-edit override (mirrors
    # _so_line_edit_gate). OPEN is allowed for any sales-orders user.
    role = g.current_user.get("role")
    if so.status == SO_PICKED and role != ROLE_ADMIN and not has_override(OVERRIDE_SO_FULL_EDIT):
        return jsonify({
            "error": "Partial-fulfill on a PICKED order requires ADMIN or so-full-edit override.",
            "current_status": so.status,
        }), 403

    bo_so_number = f"{so.so_number}-BO"
    reason_detail_base = (validated.reason_detail or "partial-fulfill").strip() or "partial-fulfill"
    username = g.current_user["username"]

    # 1. Validate every input line up-front (item exists, qty bounds).
    #    Validating first lets us reject 400 cleanly without partial
    #    inventory writes lingering in the transaction.
    line_inputs = []
    for entry in validated.lines:
        line = g.db.execute(
            text(
                "SELECT sol.so_line_id, sol.item_id, sol.quantity_ordered, "
                "       sol.quantity_picked, sol.line_number, "
                "       i.sku, i.external_id AS item_external_id "
                "  FROM sales_order_lines sol "
                "  JOIN items i ON i.item_id = sol.item_id "
                " WHERE sol.so_line_id = :sol_id AND sol.so_id = :sid "
                " FOR UPDATE OF sol"
            ),
            {"sol_id": entry.so_line_id, "sid": so_id},
        ).fetchone()
        if not line:
            return jsonify({
                "error": f"so_line_id {entry.so_line_id} not on SO {so.so_number}",
            }), 400
        unshipped = line.quantity_ordered - line.quantity_picked
        if entry.short_qty > unshipped:
            return jsonify({
                "error": (
                    f"short_qty {entry.short_qty} on line {line.line_number} "
                    f"exceeds unshipped qty ({line.quantity_ordered} ordered - "
                    f"{line.quantity_picked} picked = {unshipped})"
                ),
                "so_line_id": entry.so_line_id,
            }), 400
        line_inputs.append((entry, line))

    # 2. Apply per-line shrink + adjustment.
    short_summary = []
    for entry, line in line_inputs:
        new_qty = line.quantity_ordered - entry.short_qty
        if new_qty == 0:
            # Hard-delete shrunk-to-zero lines. Zero-qty rows break the
            # dockd payload + picker assumptions.
            g.db.execute(
                text("DELETE FROM sales_order_lines WHERE so_line_id = :sol_id"),
                {"sol_id": line.so_line_id},
            )
        else:
            g.db.execute(
                text(
                    "UPDATE sales_order_lines SET quantity_ordered = :qty "
                    " WHERE so_line_id = :sol_id"
                ),
                {"qty": new_qty, "sol_id": line.so_line_id},
            )

        adj_qty = _write_short_adjustment(
            g.db,
            item_id=line.item_id,
            warehouse_id=so.warehouse_id,
            short_qty=entry.short_qty,
            reason_detail=reason_detail_base,
            bo_so_number=bo_so_number,
            username=username,
        )

        short_summary.append({
            "so_line_id": line.so_line_id,
            "item_id": line.item_id,
            "sku": line.sku,
            "item_external_id": str(line.item_external_id),
            "line_number": line.line_number,
            "ordered_before": line.quantity_ordered,
            "short_qty": entry.short_qty,
            "ordered_after": new_qty,
            "line_deleted": new_qty == 0,
            "adjustment_qty": adj_qty,
        })

    # 3. Create the BO SO. Address fields + customer + ship_method
    #    copied from the parent; order_total / customer_shipping_paid
    #    are NULL on the BO because they were already paid on the
    #    parent (downstream revenue reports must filter via
    #    parent_so_id IS NULL or COALESCE).
    bo_memo = f"[BACKORDER from {so.so_number}]\n" + (so.memo or "")
    bo_external_id = str(uuid.uuid4())
    address_cols = ", ".join(ADDRESS_COPY_FIELDS)
    address_binds = ", ".join(f":{f}" for f in ADDRESS_COPY_FIELDS)
    address_params = {f: getattr(so, f) for f in ADDRESS_COPY_FIELDS}

    bo_row = g.db.execute(
        text(f"""
            INSERT INTO sales_orders (
                so_number, customer_name, customer_id, customer_phone,
                customer_address, status, warehouse_id, ship_method,
                ship_address, {address_cols},
                memo, order_total, customer_shipping_paid,
                order_type, parent_so_id, backorder_opened_at,
                created_by, external_id, source_system
            ) VALUES (
                :so_number, :customer_name, :customer_id, :customer_phone,
                :customer_address, :status, :warehouse_id, :ship_method,
                :ship_address, {address_binds},
                :memo, NULL, NULL,
                :order_type, :parent_so_id, NOW(),
                :created_by, :external_id, :source_system
            ) RETURNING so_id
        """),
        {
            "so_number": bo_so_number,
            "customer_name": so.customer_name,
            "customer_id": so.customer_id,
            "customer_phone": so.customer_phone,
            "customer_address": so.customer_address,
            "status": SO_WAITING_STOCK,
            "warehouse_id": so.warehouse_id,
            "ship_method": so.ship_method,
            "ship_address": so.ship_address,
            "memo": bo_memo,
            "order_type": ORDER_TYPE_BACKORDER,
            "parent_so_id": so.so_id,
            "created_by": username,
            "external_id": bo_external_id,
            "source_system": so.source_system,
            **address_params,
        },
    ).fetchone()
    bo_so_id = bo_row.so_id

    # 4. BO lines: one per shorted entry, line_number sequential.
    for idx, shrink in enumerate(short_summary, 1):
        g.db.execute(
            text(
                "INSERT INTO sales_order_lines "
                "  (so_id, item_id, quantity_ordered, line_number, status) "
                "VALUES (:sid, :iid, :qty, :ln, 'PENDING')"
            ),
            {
                "sid": bo_so_id,
                "iid": shrink["item_id"],
                "qty": shrink["short_qty"],
                "ln": idx,
            },
        )

    # 5. Audit log. Two rows in the same transaction:
    #    - Original SO: shrink summary + BO pointer.
    #    - BO SO: creation hook + parent pointer.
    write_audit_log(
        g.db,
        action_type=ACTION_PARTIAL_FULFILL,
        entity_type="SO",
        entity_id=so.so_id,
        user_id=username,
        warehouse_id=so.warehouse_id,
        details={
            "kind": "shrink",
            "bo_so_id": bo_so_id,
            "bo_so_number": bo_so_number,
            "reason_detail": reason_detail_base,
            "pre_status": so.status,
            "lines": [
                {
                    k: v for k, v in s.items()
                    if k in (
                        "so_line_id", "sku", "line_number", "ordered_before",
                        "short_qty", "ordered_after", "line_deleted",
                        "adjustment_qty",
                    )
                }
                for s in short_summary
            ],
        },
    )
    write_audit_log(
        g.db,
        action_type=ACTION_PARTIAL_FULFILL,
        entity_type="SO",
        entity_id=bo_so_id,
        user_id=username,
        warehouse_id=so.warehouse_id,
        details={
            "kind": "created_from_partial_fulfill",
            "parent_so_id": so.so_id,
            "parent_so_number": so.so_number,
            "lines": [
                {"sku": s["sku"], "qty": s["short_qty"]} for s in short_summary
            ],
        },
    )

    # 6. Emit backorder.opened on the integration_events outbox. No
    #    consumer wires this yet (the Teams adapter does; marketplace
    #    adapter is deferred to a follow-up branch), but emitting now
    #    means "wire later" does not require an event-history
    #    backfill.
    user_external_id = get_user_external_id(g.db, username)
    opened_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    emit_event(
        g.db,
        event_type="backorder.opened",
        event_version=1,
        aggregate_type="sales_order",
        aggregate_id=bo_so_id,
        aggregate_external_id=bo_external_id,
        warehouse_id=so.warehouse_id,
        source_txn_id=g.source_txn_id,
        payload={
            "backorder_so_external_id": bo_external_id,
            "backorder_so_number": bo_so_number,
            "parent_so_external_id": str(so.external_id),
            "parent_so_number": so.so_number,
            "warehouse_id": so.warehouse_id,
            "customer_name": so.customer_name,
            "items": [
                {
                    "item_external_id": s["item_external_id"],
                    "sku": s["sku"],
                    "qty": s["short_qty"],
                }
                for s in short_summary
            ],
            "opened_by_user_external_id": user_external_id,
            "opened_at": opened_at,
        },
    )

    g.db.commit()

    return jsonify({
        "original_so": {
            "so_id": so.so_id,
            "so_number": so.so_number,
            "status": so.status,
        },
        "backorder_so": {
            "so_id": bo_so_id,
            "so_number": bo_so_number,
            "status": SO_WAITING_STOCK,
            "external_id": bo_external_id,
        },
        "shrink_summary": short_summary,
    })


@admin_bp.route("/sales-orders/<int:bo_so_id>/cancel-backorder", methods=["POST"])
@require_auth
@require_admin_or_page_permission("sales-orders")
@validate_body(CancelBackorderRequest)
@with_db
def cancel_backorder(bo_so_id, validated):
    """Cancel a backorder SO. Thin wrapper that stamps
    cancellation_reason then delegates to the shared cancel service.

    The backorder.cancelled event is emitted inside
    sales_order_service.cancel_sales_order (so the parent-cancel
    cascade emits too); this endpoint only sets the reason and
    handles the BO-specific validation."""
    reason = (validated.cancellation_reason or CANCEL_REASON_OTHER).strip()
    if reason not in CANCEL_REASONS:
        return jsonify({
            "error": (
                f"Invalid cancellation_reason. Allowed: "
                f"{', '.join(CANCEL_REASONS)}"
            ),
        }), 400

    bo = g.db.execute(
        text(
            "SELECT so_id, status, parent_so_id, order_type "
            "  FROM sales_orders WHERE so_id = :sid"
        ),
        {"sid": bo_so_id},
    ).fetchone()
    if not bo:
        return jsonify({"error": "Sales order not found"}), 404
    if bo.parent_so_id is None or bo.order_type != ORDER_TYPE_BACKORDER:
        return jsonify({
            "error": (
                "Not a backorder SO. Use /cancel for non-backorder orders."
            ),
        }), 400
    if bo.status == SO_CANCELLED:
        return jsonify({
            "error": "Backorder already cancelled.",
            "current_status": bo.status,
        }), 400

    # Stamp the reason BEFORE cancel_sales_order so the service-side
    # backorder.cancelled emit sees it on the row.
    g.db.execute(
        text(
            "UPDATE sales_orders SET cancellation_reason = :reason "
            " WHERE so_id = :sid"
        ),
        {"reason": reason, "sid": bo_so_id},
    )

    try:
        result = _cancel_so(
            g.db, so_id=bo_so_id, source="admin",
            username=g.current_user["username"],
        )
    except CancelNotAllowed as exc:
        if exc.current_status == "UNKNOWN":
            return jsonify({"error": "Sales order not found"}), 404
        return jsonify({
            "error": str(exc),
            "current_status": exc.current_status,
        }), 400

    g.db.commit()
    return jsonify({
        "message": "Backorder cancelled",
        "bo_so_id": bo_so_id,
        "cancellation_reason": reason,
        "audit_log_id": result["audit_log_id"],
    })


@admin_bp.route("/backorders", methods=["GET"])
@require_auth
@require_admin_or_page_permission("backorders")
@with_db
def list_backorders():
    """List backorder SOs grouped by tab.

    Query params:
      - warehouse_id (optional): scope to one warehouse
      - tab: 'waiting' (status=WAITING_STOCK) or 'ready-to-ship'
        (status IN OPEN/PICKED/PACKED AND backorder_opened_at is set)
        Defaults to 'waiting'.

    Response carries per-BO items[] + days_waiting; the
    ready-to-ship tab also carries fulfillable_since
    (backorder_fulfillable_at). Latest open-PO ETA per item is
    omitted because purchase_order_lines does not carry an
    expected_arrival_date column yet; revisit once that's wired."""
    tab = (request.args.get("tab") or "waiting").lower()
    warehouse_id = request.args.get("warehouse_id", type=int)

    if tab == "waiting":
        status_clause = "bo.status = :waiting"
        params = {"waiting": SO_WAITING_STOCK}
    elif tab in ("ready-to-ship", "ready_to_ship", "ready"):
        status_clause = (
            "bo.status IN (:s_open, :s_picked, :s_packed) "
            "  AND bo.backorder_opened_at IS NOT NULL"
        )
        params = {"s_open": SO_OPEN, "s_picked": SO_PICKED, "s_packed": SO_PACKED}
    else:
        return jsonify({
            "error": "tab must be 'waiting' or 'ready-to-ship'",
            "got": tab,
        }), 400

    if warehouse_id:
        status_clause += " AND bo.warehouse_id = :wid"
        params["wid"] = warehouse_id

    rows = g.db.execute(
        text(
            f"""
            SELECT bo.so_id, bo.so_number, bo.warehouse_id, bo.status,
                   bo.customer_name, bo.backorder_opened_at,
                   bo.backorder_fulfillable_at,
                   parent.so_number AS parent_so_number,
                   EXTRACT(EPOCH FROM (NOW() - bo.backorder_opened_at))::bigint AS waiting_seconds
              FROM sales_orders bo
              LEFT JOIN sales_orders parent ON parent.so_id = bo.parent_so_id
             WHERE {status_clause}
               AND bo.order_type = :backorder
             ORDER BY bo.backorder_opened_at ASC
            """
        ),
        {**params, "backorder": ORDER_TYPE_BACKORDER},
    ).fetchall()

    so_ids = [r.so_id for r in rows]
    items_by_so = {}
    if so_ids:
        line_rows = g.db.execute(
            text(
                "SELECT sol.so_id, i.sku, sol.quantity_ordered "
                "  FROM sales_order_lines sol "
                "  JOIN items i ON i.item_id = sol.item_id "
                " WHERE sol.so_id = ANY(:so_ids) "
                " ORDER BY sol.line_number"
            ),
            {"so_ids": so_ids},
        ).fetchall()
        for lr in line_rows:
            items_by_so.setdefault(lr.so_id, []).append({
                "sku": lr.sku,
                "qty": lr.quantity_ordered,
            })

    return jsonify({
        "tab": tab,
        "backorders": [
            {
                "so_id":           r.so_id,
                "so_number":       r.so_number,
                "parent_so_number": r.parent_so_number,
                "warehouse_id":    r.warehouse_id,
                "status":          r.status,
                "customer_name":   r.customer_name,
                "items":           items_by_so.get(r.so_id, []),
                "days_waiting":    (int(r.waiting_seconds) // 86400) if r.waiting_seconds else 0,
                "backorder_opened_at": (
                    r.backorder_opened_at.isoformat()
                    if r.backorder_opened_at else None
                ),
                "fulfillable_since": (
                    r.backorder_fulfillable_at.isoformat()
                    if r.backorder_fulfillable_at else None
                ),
            }
            for r in rows
        ],
    })


# ── Sales Order Lines (add / update / remove) ────────────────────────────────
#
# Mirrors the PO line surface shape but layers an
# allocation-release pass because SO lines can have inventory committed
# to them post-OPEN. Status gate is the same three-tier model as
# update_sales_order: OPEN for any sales-orders user, anything else
# requires so-full-edit override, SHIPPED + CANCELLED are terminal.
#
# Pick / pack / ship state on the line is treated as a hard wall:
# quantity_picked > 0 means units have already left their source bin;
# quantity_shipped > 0 means the load has left the building. Either
# blocks edits even with the override because reconciling them needs
# the unwind paths in sales_order_service, not a direct line mutation.

SO_LINE_EDIT_TERMINAL_STATUSES = {SO_SHIPPED, SO_CANCELLED}


def _so_line_edit_gate(so):
    """Returns (allowed: bool, error_response_tuple_or_None).

    Encapsulates the status-tier + override check that all SO line
    CRUD endpoints share so the three handlers stay in lock-step.
    """
    if so.status in SO_LINE_EDIT_TERMINAL_STATUSES:
        return False, (jsonify({
            "error": f"Cannot edit lines on a {so.status} order",
            "current_status": so.status,
        }), 400)
    role = g.current_user.get("role")
    if role == ROLE_ADMIN:
        return True, None
    if so.status == SO_OPEN:
        return True, None
    if has_override(OVERRIDE_SO_FULL_EDIT):
        return True, None
    return False, (jsonify({
        "error": "Line edits past OPEN require ADMIN or so-full-edit override",
        "current_status": so.status,
    }), 403)


def _release_so_line_allocation(db, so_line_id: int, warehouse_id: int) -> int:
    """Release allocations attached to a single SO line.

    Walks the line's pending pick_tasks, decrements
    inventory.quantity_allocated by each task's quantity_to_pick, zeroes
    out sales_order_lines.quantity_allocated, then deletes the line's
    pending pick_tasks rows. The pick_batch_orders row is left
    untouched because it may still cover the SO's other lines.

    Returns the total quantity released. Caller is responsible for
    writing the audit row and for committing the transaction.

    Mirrors the per-line subset of _unwind_allocated in
    sales_order_service so the bin-level effect of a line shrink/delete
    is identical to a full-SO cancel at the OPEN/PICKING boundary.
    """
    line = db.execute(
        text(
            "SELECT so_line_id, item_id, quantity_allocated "
            "  FROM sales_order_lines WHERE so_line_id = :sol_id"
        ),
        {"sol_id": so_line_id},
    ).fetchone()
    if not line or line.quantity_allocated == 0:
        return 0

    tasks = db.execute(
        text(
            "SELECT bin_id, quantity_to_pick FROM pick_tasks "
            " WHERE so_line_id = :sol_id AND status = :task_status"
        ),
        {"sol_id": so_line_id, "task_status": TASK_PENDING},
    ).fetchall()
    released_total = 0
    for task in tasks:
        db.execute(
            text(
                "UPDATE inventory "
                "   SET quantity_allocated = GREATEST(0, quantity_allocated - :qty) "
                " WHERE item_id = :iid AND bin_id = :bid"
            ),
            {"qty": task.quantity_to_pick, "iid": line.item_id, "bid": task.bin_id},
        )
        released_total += task.quantity_to_pick

    db.execute(
        text(
            "DELETE FROM pick_tasks "
            " WHERE so_line_id = :sol_id AND status = :task_status"
        ),
        {"sol_id": so_line_id, "task_status": TASK_PENDING},
    )
    db.execute(
        text(
            "UPDATE sales_order_lines SET quantity_allocated = 0 "
            " WHERE so_line_id = :sol_id"
        ),
        {"sol_id": so_line_id},
    )
    return released_total


@admin_bp.route("/sales-orders/<int:so_id>/lines", methods=["POST"])
@require_auth
@require_admin_or_page_permission("sales-orders")
@validate_body(AddSalesOrderLineRequest)
@with_db
def add_sales_order_line(so_id, validated):
    data = validated.model_dump()

    so = g.db.execute(
        text("SELECT so_id, status, warehouse_id FROM sales_orders WHERE so_id = :sid FOR UPDATE"),
        {"sid": so_id},
    ).fetchone()
    if not so:
        return jsonify({"error": "Sales order not found"}), 404

    allowed, err = _so_line_edit_gate(so)
    if not allowed:
        return err

    item = g.db.execute(
        text("SELECT item_id, sku FROM items WHERE item_id = :iid"),
        {"iid": data["item_id"]},
    ).fetchone()
    if not item:
        return jsonify({"error": f"Item {data['item_id']} not found"}), 400

    # Duplicate-item guard mirrors the PO surface: allocation +
    # pick-batch creation paths fetch by (so_id, item_id) and cannot
    # disambiguate two rows with the same item_id.
    dup = g.db.execute(
        text("SELECT 1 FROM sales_order_lines WHERE so_id = :sid AND item_id = :iid"),
        {"sid": so_id, "iid": data["item_id"]},
    ).fetchone()
    if dup:
        return jsonify({"error": f"Item {item.sku} is already on this SO"}), 400

    next_ln = g.db.execute(
        text("SELECT COALESCE(MAX(line_number), 0) + 1 FROM sales_order_lines WHERE so_id = :sid"),
        {"sid": so_id},
    ).scalar()

    result = g.db.execute(
        text(
            """
            INSERT INTO sales_order_lines
                (so_id, item_id, quantity_ordered, line_number)
            VALUES (:sid, :iid, :qty, :ln)
            RETURNING so_line_id
            """
        ),
        {
            "sid": so_id, "iid": data["item_id"],
            "qty": data["quantity_ordered"], "ln": next_ln,
        },
    )
    so_line_id = result.fetchone()[0]

    write_audit_log(
        g.db,
        ACTION_SO_LINE_ADDED,
        "SO_LINE",
        so_line_id,
        g.current_user["user_id"],
        so.warehouse_id,
        details={
            "so_id": so_id, "sku": item.sku,
            "quantity_ordered": data["quantity_ordered"],
        },
    )
    g.db.commit()

    return jsonify({
        "so_line_id": so_line_id, "so_id": so_id,
        "item_id": data["item_id"], "sku": item.sku,
        "quantity_ordered": data["quantity_ordered"],
        "quantity_allocated": 0, "quantity_picked": 0,
        "quantity_packed": 0, "quantity_shipped": 0,
        "line_number": next_ln,
    }), 201


@admin_bp.route("/sales-orders/<int:so_id>/lines/<int:so_line_id>", methods=["PATCH"])
@require_auth
@require_admin_or_page_permission("sales-orders")
@validate_body(UpdateSalesOrderLineRequest)
@with_db
def update_sales_order_line(so_id, so_line_id, validated):
    data = validated.model_dump()

    so = g.db.execute(
        text("SELECT so_id, status, warehouse_id FROM sales_orders WHERE so_id = :sid FOR UPDATE"),
        {"sid": so_id},
    ).fetchone()
    if not so:
        return jsonify({"error": "Sales order not found"}), 404

    allowed, err = _so_line_edit_gate(so)
    if not allowed:
        return err

    line = g.db.execute(
        text(
            """
            SELECT sol.so_line_id, sol.quantity_ordered, sol.quantity_allocated,
                   sol.quantity_picked, sol.quantity_packed, sol.quantity_shipped,
                   i.sku
              FROM sales_order_lines sol JOIN items i ON i.item_id = sol.item_id
             WHERE sol.so_line_id = :sol_id AND sol.so_id = :sid
            """
        ),
        {"sol_id": so_line_id, "sid": so_id},
    ).fetchone()
    if not line:
        return jsonify({"error": "Line not found"}), 404

    new_qty = data["quantity_ordered"]
    # Picked / packed / shipped units already left their source bin or
    # the building. Going below those numbers would orphan physical
    # movements; require the operator to unwind via the dedicated paths
    # (short-pick / void-ship) before shrinking the line.
    picked_or_packed = max(line.quantity_picked, line.quantity_packed)
    if new_qty < picked_or_packed:
        return jsonify({
            "error": (
                f"quantity_ordered ({new_qty}) cannot be less than "
                f"already-picked/packed quantity ({picked_or_packed})"
            ),
        }), 400
    if new_qty < line.quantity_shipped:
        return jsonify({
            "error": (
                f"quantity_ordered ({new_qty}) cannot be less than "
                f"already-shipped quantity ({line.quantity_shipped})"
            ),
        }), 400

    released = 0
    # Shrinking below quantity_allocated releases the full line
    # allocation; re-allocation happens at next pick-batch create.
    # Going from N to a smaller N' that is still >= quantity_allocated
    # leaves the existing allocation in place because the picker can
    # still fulfil it.
    if new_qty < line.quantity_allocated and line.quantity_allocated > 0:
        released = _release_so_line_allocation(g.db, so_line_id, so.warehouse_id)
        if released > 0:
            write_audit_log(
                g.db,
                ACTION_SO_ALLOCATION_RELEASED,
                "SO_LINE",
                so_line_id,
                g.current_user["user_id"],
                so.warehouse_id,
                details={
                    "so_id": so_id, "sku": line.sku,
                    "released_quantity": released,
                    "reason": "line_quantity_reduced",
                },
            )

    g.db.execute(
        text(
            "UPDATE sales_order_lines SET quantity_ordered = :qty "
            " WHERE so_line_id = :sol_id"
        ),
        {"qty": new_qty, "sol_id": so_line_id},
    )

    write_audit_log(
        g.db,
        ACTION_SO_LINE_UPDATED,
        "SO_LINE",
        so_line_id,
        g.current_user["user_id"],
        so.warehouse_id,
        details={
            "so_id": so_id, "sku": line.sku,
            "old_quantity_ordered": line.quantity_ordered,
            "new_quantity_ordered": new_qty,
        },
    )
    g.db.commit()
    return jsonify({
        "so_line_id": so_line_id,
        "quantity_ordered": new_qty,
        "released_allocation": released,
    })


@admin_bp.route("/sales-orders/<int:so_id>/lines/<int:so_line_id>", methods=["DELETE"])
@require_auth
@require_admin_or_page_permission("sales-orders")
@with_db
def delete_sales_order_line(so_id, so_line_id):
    so = g.db.execute(
        text("SELECT so_id, status, warehouse_id FROM sales_orders WHERE so_id = :sid FOR UPDATE"),
        {"sid": so_id},
    ).fetchone()
    if not so:
        return jsonify({"error": "Sales order not found"}), 404

    allowed, err = _so_line_edit_gate(so)
    if not allowed:
        return err

    line = g.db.execute(
        text(
            """
            SELECT sol.so_line_id, sol.quantity_ordered, sol.quantity_allocated,
                   sol.quantity_picked, sol.quantity_packed, sol.quantity_shipped,
                   i.sku
              FROM sales_order_lines sol JOIN items i ON i.item_id = sol.item_id
             WHERE sol.so_line_id = :sol_id AND sol.so_id = :sid
            """
        ),
        {"sol_id": so_line_id, "sid": so_id},
    ).fetchone()
    if not line:
        return jsonify({"error": "Line not found"}), 404

    # Picked / packed / shipped units block deletion at any tier. To
    # remove these the operator must unwind through the dedicated
    # surfaces (short-pick / void-ship) so inventory movements stay
    # consistent.
    if line.quantity_picked > 0 or line.quantity_packed > 0 or line.quantity_shipped > 0:
        return jsonify({
            "error": (
                "Line has picked/packed/shipped units; unwind via the "
                "ship-void or short-pick path before removing the line"
            ),
            "quantity_picked": line.quantity_picked,
            "quantity_packed": line.quantity_packed,
            "quantity_shipped": line.quantity_shipped,
        }), 400

    released = 0
    if line.quantity_allocated > 0:
        released = _release_so_line_allocation(g.db, so_line_id, so.warehouse_id)
        if released > 0:
            write_audit_log(
                g.db,
                ACTION_SO_ALLOCATION_RELEASED,
                "SO_LINE",
                so_line_id,
                g.current_user["user_id"],
                so.warehouse_id,
                details={
                    "so_id": so_id, "sku": line.sku,
                    "released_quantity": released,
                    "reason": "line_deleted",
                },
            )

    g.db.execute(
        text("DELETE FROM sales_order_lines WHERE so_line_id = :sol_id"),
        {"sol_id": so_line_id},
    )

    write_audit_log(
        g.db,
        ACTION_SO_LINE_REMOVED,
        "SO_LINE",
        so_line_id,
        g.current_user["user_id"],
        so.warehouse_id,
        details={
            "so_id": so_id, "sku": line.sku,
            "quantity_ordered": line.quantity_ordered,
        },
    )
    g.db.commit()
    return jsonify({
        "so_line_id": so_line_id, "deleted": True,
        "released_allocation": released,
    })


# ── Short Picks Report ────────────────────────────────────────────────────────

@admin_bp.route("/short-picks", methods=["GET"])
@require_auth
@require_admin_or_page_permission("sales-orders")
@with_db
def get_short_picks():
    """Return recent short pick events from the audit log."""
    days = request.args.get("days", 30, type=int)
    warehouse_id = request.args.get("warehouse_id", type=int)
    wh_clause = "AND a.warehouse_id = :wid" if warehouse_id else ""
    params = {"days": days}
    if warehouse_id:
        params["wid"] = warehouse_id

    params["action_type"] = ACTION_PICK
    rows = g.db.execute(
        text(f"""
            SELECT a.log_id, a.user_id, a.created_at,
                   a.details->>'sku' AS sku,
                   (a.details->>'quantity_to_pick')::int AS qty_expected,
                   (a.details->>'quantity_picked')::int AS qty_picked,
                   (a.details->>'shortage')::int AS shortage,
                   b.bin_code,
                   a.details->>'batch_id' AS batch_id
            FROM audit_log a
            LEFT JOIN bins b ON b.bin_id = (a.details->>'bin_id')::int
            WHERE a.action_type = :action_type
              AND a.details->>'type' = 'SHORT_PICK'
              AND a.created_at >= NOW() - make_interval(days => :days)
              {wh_clause}
            ORDER BY a.created_at DESC
            LIMIT 100
        """),
        params,
    ).fetchall()

    return jsonify({
        "short_picks": [
            {
                "log_id": r.log_id,
                "user": r.user_id,
                "sku": r.sku,
                "qty_expected": r.qty_expected,
                "qty_picked": r.qty_picked,
                "shortage": r.shortage,
                "bin_code": r.bin_code,
                "batch_id": r.batch_id,
                "timestamp": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
        "total": len(rows),
    })
