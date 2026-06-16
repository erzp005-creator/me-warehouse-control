"""
Status constants used across the Sentry WMS API.
"""

import os

# Purchase Order statuses
PO_OPEN = "OPEN"
PO_PARTIAL = "PARTIAL"
PO_RECEIVED = "RECEIVED"
PO_CLOSED = "CLOSED"

# Purchase Order Line statuses
POL_PENDING = "PENDING"
POL_PARTIAL = "PARTIAL"
POL_RECEIVED = "RECEIVED"

# Sales Order statuses
SO_OPEN = "OPEN"
SO_PICKED = "PICKED"
SO_PACKED = "PACKED"
SO_SHIPPED = "SHIPPED"
SO_CANCELLED = "CANCELLED"
# Company-wide timezone for operator-facing calendar dates (e.g. the
# Shipped Date on the SO modal). Timestamps are stored in UTC; this is
# the single zone used to convert them to the local calendar date
# operators reason about, and to anchor manually-entered dates.
# Deployment-configurable; defaults to UTC. Single-zone assumption;
# switch to per-row warehouses.timezone if that ever changes.
COMPANY_TIMEZONE = os.environ.get("SENTRY_COMPANY_TIMEZONE", "UTC")


# Pick Batch statuses
BATCH_OPEN = "OPEN"
BATCH_IN_PROGRESS = "IN_PROGRESS"
BATCH_COMPLETED = "COMPLETED"
BATCH_CANCELLED = "CANCELLED"

# Pick Task statuses
TASK_PENDING = "PENDING"
TASK_PICKED = "PICKED"
TASK_SHORT = "SHORT"
TASK_SKIPPED = "SKIPPED"
# so-refinement: an operator-reverted PICKED task. Distinct from
# PENDING so a subsequent revert prompt does not re-offer the same
# task, and from PICKED so dashboards do not double-count.
TASK_RELEASED = "RELEASED"

# Cycle Count statuses
COUNT_PENDING = "PENDING"
COUNT_IN_PROGRESS = "IN_PROGRESS"
COUNT_COMPLETED = "COMPLETED"
COUNT_VARIANCE = "VARIANCE"

# Inventory Adjustment statuses
ADJ_PENDING = "PENDING"
ADJ_APPROVED = "APPROVED"
ADJ_REJECTED = "REJECTED"

# Audit Log action types
ACTION_RECEIVE = "RECEIVE"
ACTION_RECEIVE_CANCEL = "RECEIVE_CANCEL"
ACTION_PUTAWAY = "PUTAWAY"
ACTION_PICK = "PICK"
ACTION_PACK = "PACK"
ACTION_SHIP = "SHIP"
# v1.9.0 dockd: void of a previously-shipped order. Reverts the SO and
# the matching item_fulfillment row to pre_ship_status (PICKED or PACKED).
ACTION_SHIP_VOID = "SHIP_VOID"
# v1.10.0 POS endpoint surface. Counter sale (atomic SO-create +
# inventory decrement) and refund (credit-memo SO + inventory re-
# increment). One audit_log row per successful checkout / refund.
ACTION_POS_CHECKOUT = "POS_CHECKOUT"
ACTION_POS_REFUND = "POS_REFUND"
# v1.9.0: SO cancellation. Initiated by ERP via inbound or by an admin
# operator. Pre-PICKED states release allocation; PICKED/PACKED states
# revert inventory to the default receiving bin.
ACTION_CANCEL = "CANCEL"
ACTION_TRANSFER = "TRANSFER"
ACTION_ADJUST = "ADJUST"
ACTION_COUNT = "COUNT"

# v1.5.1 V-208 (#141): wms_tokens lifecycle actions. Admin token CRUD
# (issue, rotate, revoke, delete) writes one audit_log row per call
# so post-incident forensics can reconstruct "who issued what and
# when" even if the DB row itself is later deleted. The v1.4 hash
# chain trigger on audit_log makes the trail tamper-evident.
# Plaintext token values NEVER appear in `details`; scope snapshots
# do, so delete can be audited after the row is gone.
ACTION_TOKEN_ISSUE = "TOKEN_ISSUE"
ACTION_TOKEN_ROTATE = "TOKEN_ROTATE"
ACTION_TOKEN_REVOKE = "TOKEN_REVOKE"
ACTION_TOKEN_DELETE = "TOKEN_DELETE"

# v1.5.1 V-221 (#154): consumer_groups + connector-registry admin
# actions. Structurally identical to the V-208 token CRUD audit
# coverage but lower severity -- consumer_groups do not hold auth
# material, so a compromise here causes data-flow misdirection
# (V-207 replay, V-204 subscription tampering) rather than an auth
# bypass. Worth filing for forensic symmetry: without these writes,
# an attacker could delete + recreate a consumer_group with a
# tampered subscription (V-204) and leave no audit trace.
#
# Entity-id convention: consumer_group_id and connector_id are
# VARCHAR so they cannot fit audit_log.entity_id (INT NOT NULL).
# Writes use entity_id=0 as a sentinel and carry the real string
# id in details so investigators can still bind actions to rows.
ACTION_CONNECTOR_REGISTRY_CREATE = "CONNECTOR_REGISTRY_CREATE"
ACTION_CONNECTOR_REGISTRY_UPDATE = "CONNECTOR_REGISTRY_UPDATE"
ACTION_CONNECTOR_REGISTRY_DELETE = "CONNECTOR_REGISTRY_DELETE"
ACTION_CONSUMER_GROUP_CREATE = "CONSUMER_GROUP_CREATE"
ACTION_CONSUMER_GROUP_UPDATE = "CONSUMER_GROUP_UPDATE"
ACTION_CONSUMER_GROUP_DELETE = "CONSUMER_GROUP_DELETE"

# v1.6.0 outbound webhook subscription CRUD audit coverage. Same
# shape as the v1.5 token CRUD writes: one audit row per mutation,
# scope snapshot in details so post-incident forensics survive a
# hard delete. Plaintext webhook secrets NEVER appear in details;
# the row carries display_name + delivery_url + filter + ceilings
# + rate so an investigator can reconstruct what was created and
# under what bounds. entity_id holds a stable surrogate (the audit
# table column is INT; subscription_id is UUID, so writes use a
# sentinel and carry the UUID under details.subscription_id).
ACTION_WEBHOOK_SUBSCRIPTION_CREATE = "WEBHOOK_SUBSCRIPTION_CREATE"
ACTION_WEBHOOK_SUBSCRIPTION_UPDATE = "WEBHOOK_SUBSCRIPTION_UPDATE"
ACTION_WEBHOOK_SUBSCRIPTION_DELETE_SOFT = "WEBHOOK_SUBSCRIPTION_DELETE_SOFT"
ACTION_WEBHOOK_SUBSCRIPTION_DELETE_HARD = "WEBHOOK_SUBSCRIPTION_DELETE_HARD"
ACTION_WEBHOOK_SECRET_ROTATE = "WEBHOOK_SECRET_ROTATE"
ACTION_WEBHOOK_DELIVERY_REPLAY_SINGLE = "WEBHOOK_DELIVERY_REPLAY_SINGLE"
ACTION_WEBHOOK_DELIVERY_REPLAY_BATCH = "WEBHOOK_DELIVERY_REPLAY_BATCH"
# #232: dispatcher auto-pause when subscription_filter fails
# Pydantic validation. user_id is the daemon's identity ("system");
# details.subscription_id + details.parse_error capture the
# offending row + the recoverable error so an operator can find
# the bad column in audit_log without grepping daemon logs.
ACTION_WEBHOOK_SUBSCRIPTION_AUTO_PAUSE = "WEBHOOK_SUBSCRIPTION_AUTO_PAUSE"

# v1.8.0 (#288) sales_order address edits. One audit row per edited
# field carrying {field_changed, old_value, new_value} in details so
# investigators can reconstruct who changed what without scanning the
# 16-column row diff. PII-careful: only changed fields are recorded,
# not the full address.
ACTION_SO_ADDRESS_EDITED = "SO_ADDRESS_EDITED"

# v1.8.0 (#290) transfer order lifecycle. Same audit shape as the
# cycle count adjustment surface: one row per state transition;
# details JSONB carries the surrounding context. entity_type is 'TO'
# for header actions, 'TO_LINE' for line actions, 'TO_APPROVAL' for
# approval actions. The audit_log V-025 hash chain extends through
# every TO surface so post-incident forensics can reconstruct the
# full lifecycle.
ACTION_TO_CREATED           = "TO_CREATED"
ACTION_TO_LINE_PICKED       = "TO_LINE_PICKED"
ACTION_TO_SUBMITTED         = "TO_SUBMITTED"
ACTION_TO_APPROVED          = "TO_APPROVED"
ACTION_TO_REJECTED          = "TO_REJECTED"
ACTION_TO_LINE_SHORT_CLOSED = "TO_LINE_SHORT_CLOSED"
ACTION_TO_CANCELLED         = "TO_CANCELLED"
ACTION_TO_DELETED           = "TO_DELETED"
ACTION_TO_CLOSED            = "TO_CLOSED"

# Bin types
BIN_STAGING = "Staging"
BIN_PICKABLE_STAGING = "PickableStaging"
BIN_PICKABLE = "Pickable"

# User roles
ROLE_ADMIN = "ADMIN"
ROLE_USER = "USER"

# mig 061: source of truth for the per-page web-admin
# permission grid. Keys MATCH the React admin URL slugs so the sidebar
# filter and the @require_admin_or_page_permission decorator can both
# read straight off this list. Adding a new admin page is a two-step
# change: append the key here and grant it to any users who need it.
ALL_PAGE_KEYS = (
    "dashboard",
    "inventory", "cycle-counts", "count-approvals",
    "purchase-orders", "receiving", "putaway",
    "sales-orders",
    # Picking Tickets (Outbound): the printable packing-slip queue and
    # the per-SO / Print All print views. Holders see the /picking-tickets
    # page and can mark tickets printed.
    "picking-tickets",
    # Picking Batches (Outbound): lists the active pick batches that hold
    # the cross-pick lock and lets an admin release a stuck one. Holders
    # see the /picking-batches page plus the SO batch-release action.
    "picking-batches",
    "picking", "packing", "shipping",
    "items",
    "adjustments", "inter-warehouse-transfers", "transfer-orders",
    "warehouses", "bins", "zones", "preferred-bins",
    "users", "api-tokens", "inbound", "consumer-groups",
    "webhooks", "audit-log", "imports", "integrations", "settings",
)

# Override grants (mig 062): feature-flag grants that ride on the same
# user_page_permissions table but are not pages in the sidebar. Granting
# one lifts the status gate on SO edits so a non-admin operator can
# repair an order past the OPEN window (e.g. fix a customer-supplied
# typo on a CLOSED PO or repoint an SO's source_system after an ERP
# mis-tag). ADMIN bypasses these checks; non-admins must hold the
# override key. Kept out of ALL_PAGE_KEYS so the sidebar permission
# grid renders them in a separate "Overrides" group.
OVERRIDE_SO_FULL_EDIT = "so-full-edit"
ALL_OVERRIDE_KEYS = (
    OVERRIDE_SO_FULL_EDIT,
)

# SO mutation audit actions (mig 062). Mirror the PO line
# CRUD coverage so the "what was on this order before it shipped"
# question survives any post-OPEN edit. details JSONB carries the
# before/after diff so an investigator can reconstruct the change
# without scanning the row itself.
ACTION_SO_LINE_ADDED = "SO_LINE_ADDED"
ACTION_SO_LINE_UPDATED = "SO_LINE_UPDATED"
ACTION_SO_LINE_REMOVED = "SO_LINE_REMOVED"
ACTION_SO_HEADER_EDITED = "SO_HEADER_EDITED"
ACTION_SO_SOURCE_SYSTEM_REASSIGNED = "SO_SOURCE_SYSTEM_REASSIGNED"
ACTION_SO_ALLOCATION_RELEASED = "SO_ALLOCATION_RELEASED"
# so-refinement: admin reverts an SO from PICKED/PACKED/SHIPPED back
# to an earlier status. One STATUS_REVERTED row per request carries
# {from_status, to_status}; the per-effect rows below carry the line
# / pick_task detail so investigators can reconstruct the unwind.
ACTION_SO_STATUS_REVERTED = "SO_STATUS_REVERTED"
ACTION_SO_PICK_RELEASED = "SO_PICK_RELEASED"
ACTION_SO_UNPACKED = "SO_UNPACKED"
ACTION_SO_UNSHIPPED = "SO_UNSHIPPED"
