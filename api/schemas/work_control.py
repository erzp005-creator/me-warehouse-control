"""Validation models for ME Warehouse Control.

This module deliberately models execution facts only. It does not mutate the
canonical inventory, purchase-order or sales-order ledgers.
"""

from datetime import date, datetime
from typing import List, Optional
from urllib.parse import urlparse
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


TASK_TYPES = ("PICKING", "PACKING", "RECEIVING", "PUTAWAY", "STOCK_CHECK", "OTHER")
TASK_ACTIONS = ("START", "PAUSE", "RESUME", "COMPLETE", "EXCEPTION", "CANCEL")
WORKER_AVAILABILITY_STATUSES = ("AVAILABLE", "BREAK", "OFF_DUTY")
ERROR_SEVERITIES = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
ERROR_STATUSES = ("PENDING", "CONFIRMED", "DISMISSED")
RESPONSIBILITIES = (
    "UNCONFIRMED", "PICKER", "PACKER", "BOTH", "SUPPLIER",
    "SOURCE_DATA", "SYSTEM", "UNKNOWN",
)


class BatchOrderEntry(BaseModel):
    order_number: str = Field(..., min_length=1, max_length=128)
    courier_barcode: Optional[str] = Field(None, min_length=1, max_length=128)
    platform: Optional[str] = Field(None, max_length=32)
    sku_count: int = Field(0, ge=0, le=100000)
    unit_count: int = Field(0, ge=0, le=1000000)


class CreateBatchRequest(BaseModel):
    warehouse_id: int = Field(..., gt=0)
    source_system: str = Field("manual", min_length=1, max_length=64)
    pack_note_ref: str = Field(..., min_length=1, max_length=128)
    platform: Optional[str] = Field(None, max_length=32)
    priority: int = Field(50, ge=0, le=100)
    declared_order_count: Optional[int] = Field(None, ge=1, le=50)
    orders: List[BatchOrderEntry] = Field(..., min_length=1, max_length=50)
    task_types: List[str] = Field(default_factory=lambda: ["PICKING", "PACKING"], min_length=1)

    @field_validator("task_types")
    @classmethod
    def validate_task_types(cls, values):
        normalized = [v.upper() for v in values]
        unknown = [v for v in normalized if v not in TASK_TYPES]
        if unknown:
            raise ValueError(f"unsupported task types: {unknown}")
        if len(set(normalized)) != len(normalized):
            raise ValueError("task_types must not contain duplicates")
        return normalized

    @model_validator(mode="after")
    def validate_unique_orders(self):
        order_numbers = [o.order_number for o in self.orders]
        if len(set(order_numbers)) != len(order_numbers):
            raise ValueError("orders must have unique order_number values")
        barcodes = [o.courier_barcode for o in self.orders if o.courier_barcode]
        if len(set(barcodes)) != len(barcodes):
            raise ValueError("orders must have unique courier_barcode values")
        if (
            self.declared_order_count is not None
            and self.declared_order_count < len(self.orders)
        ):
            raise ValueError(
                "declared_order_count cannot be smaller than the listed orders"
            )
        return self


class CreateTaskRequest(BaseModel):
    warehouse_id: int = Field(..., gt=0)
    task_type: str
    batch_id: Optional[int] = Field(None, gt=0)
    priority: int = Field(50, ge=0, le=100)
    assigned_to: Optional[str] = Field(None, max_length=100)
    source_ref: Optional[str] = Field(None, max_length=128)
    order_count: int = Field(0, ge=0, le=100000)
    sku_count: int = Field(0, ge=0, le=100000)
    unit_count: int = Field(0, ge=0, le=1000000)
    complexity_level: int = Field(2, ge=1, le=5)
    complexity_note: Optional[str] = Field(None, max_length=500)
    idempotency_key: Optional[str] = Field(None, max_length=128)

    @field_validator("task_type")
    @classmethod
    def validate_task_type(cls, value):
        value = value.upper()
        if value not in TASK_TYPES:
            raise ValueError(f"task_type must be one of {', '.join(TASK_TYPES)}")
        return value


class ClaimNextTaskRequest(BaseModel):
    warehouse_id: int = Field(..., gt=0)
    task_types: Optional[List[str]] = None
    device_id: Optional[str] = Field(None, max_length=100)

    @field_validator("task_types")
    @classmethod
    def validate_task_types(cls, values):
        if values is None:
            return values
        normalized = [v.upper() for v in values]
        unknown = [v for v in normalized if v not in TASK_TYPES]
        if unknown:
            raise ValueError(f"unsupported task types: {unknown}")
        return list(dict.fromkeys(normalized))


class WorkerAvailabilityRequest(BaseModel):
    warehouse_id: int = Field(..., gt=0)
    status: str
    daily_capacity_minutes: int = Field(480, ge=60, le=720)
    status_note: Optional[str] = Field(None, max_length=500)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value):
        value = value.upper()
        if value not in WORKER_AVAILABILITY_STATUSES:
            raise ValueError(
                "status must be AVAILABLE, BREAK or OFF_DUTY"
            )
        return value


class DispatchRunRequest(BaseModel):
    warehouse_id: int = Field(..., gt=0)


class AssignTaskRequest(BaseModel):
    assigned_to: Optional[str] = Field(None, min_length=1, max_length=100)
    reason: Optional[str] = Field(None, max_length=500)


class TaskTransitionRequest(BaseModel):
    action: str
    reason_code: Optional[str] = Field(None, max_length=64)
    notes: Optional[str] = Field(None, max_length=1000)
    device_id: Optional[str] = Field(None, max_length=100)
    claim_next: bool = False
    next_task_types: Optional[List[str]] = None

    @field_validator("action")
    @classmethod
    def validate_action(cls, value):
        value = value.upper()
        if value not in TASK_ACTIONS:
            raise ValueError(f"action must be one of {', '.join(TASK_ACTIONS)}")
        return value

    @field_validator("next_task_types")
    @classmethod
    def validate_next_types(cls, values):
        if values is None:
            return values
        normalized = [v.upper() for v in values]
        unknown = [v for v in normalized if v not in TASK_TYPES]
        if unknown:
            raise ValueError(f"unsupported task types: {unknown}")
        return list(dict.fromkeys(normalized))


class VerifyTaskScanRequest(BaseModel):
    barcode: str = Field(..., min_length=1, max_length=128)
    device_id: Optional[str] = Field(None, max_length=100)


class CreateErrorRequest(BaseModel):
    warehouse_id: int = Field(..., gt=0)
    task_id: Optional[int] = Field(None, gt=0)
    batch_id: Optional[int] = Field(None, gt=0)
    batch_order_id: Optional[int] = Field(None, gt=0)
    error_type: str = Field(..., min_length=1, max_length=40)
    severity: str = Field("MEDIUM")
    discovered_stage: Optional[str] = Field(None, max_length=24)
    courier_barcode: Optional[str] = Field(None, max_length=128)
    order_number: Optional[str] = Field(None, max_length=128)
    sku: Optional[str] = Field(None, max_length=128)
    quantity: Optional[int] = Field(None, gt=0, le=1000000)
    description: Optional[str] = Field(None, max_length=2000)

    @field_validator("severity")
    @classmethod
    def validate_severity(cls, value):
        value = value.upper()
        if value not in ERROR_SEVERITIES:
            raise ValueError(f"severity must be one of {', '.join(ERROR_SEVERITIES)}")
        return value


class ReviewErrorRequest(BaseModel):
    status: str
    responsibility: str
    resolution_notes: Optional[str] = Field(None, max_length=2000)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value):
        value = value.upper()
        if value not in ERROR_STATUSES or value == "PENDING":
            raise ValueError("status must be CONFIRMED or DISMISSED")
        return value

    @field_validator("responsibility")
    @classmethod
    def validate_responsibility(cls, value):
        value = value.upper()
        if value not in RESPONSIBILITIES:
            raise ValueError(f"responsibility must be one of {', '.join(RESPONSIBILITIES)}")
        return value


class ReceivingDraftLineEntry(BaseModel):
    sku: str = Field(..., min_length=1, max_length=128)
    item_name: Optional[str] = Field(None, min_length=1, max_length=500)
    expected_quantity: Optional[int] = Field(None, ge=0, le=1000000)
    received_quantity: int = Field(..., ge=0, le=1000000)
    good_quantity: int = Field(..., ge=0, le=1000000)
    damaged_quantity: int = Field(0, ge=0, le=1000000)
    notes: Optional[str] = Field(None, max_length=1000)

    @model_validator(mode="after")
    def validate_quantities(self):
        if self.good_quantity + self.damaged_quantity != self.received_quantity:
            raise ValueError("good_quantity + damaged_quantity must equal received_quantity")
        return self

    @field_validator("sku", mode="before")
    @classmethod
    def normalize_sku(cls, value):
        return value.strip().upper()

    @field_validator("item_name", mode="before")
    @classmethod
    def clean_item_name(cls, value):
        return value.strip() if value is not None else value


class CreateReceivingDraftRequest(BaseModel):
    warehouse_id: int = Field(..., gt=0)
    task_id: Optional[int] = Field(None, gt=0)
    source_system: str = Field("manual", min_length=1, max_length=64)
    po_number: Optional[str] = Field(None, max_length=128)
    supplier_ref: Optional[str] = Field(None, max_length=128)
    notes: Optional[str] = Field(None, max_length=2000)
    lines: List[ReceivingDraftLineEntry] = Field(..., min_length=1, max_length=1000)


class SubmitReceivingDraftRequest(BaseModel):
    claim_next: bool = True
    next_task_types: Optional[List[str]] = None
    device_id: Optional[str] = Field(None, max_length=100)

    @field_validator("next_task_types")
    @classmethod
    def validate_next_types(cls, values):
        if values is None:
            return values
        normalized = [v.upper() for v in values]
        unknown = [v for v in normalized if v not in TASK_TYPES]
        if unknown:
            raise ValueError(f"unsupported task types: {unknown}")
        return list(dict.fromkeys(normalized))


class ReviewReceivingDraftRequest(BaseModel):
    status: str
    review_notes: Optional[str] = Field(None, max_length=2000)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value):
        value = value.upper()
        if value not in ("APPROVED", "REJECTED", "POSTED"):
            raise ValueError("status must be APPROVED, REJECTED or POSTED")
        return value


def _require_sitegiant_url(value, *, image=False):
    if value is None:
        return value
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower()
    allowed = hostname == "sitegiant.co" or hostname.endswith(".sitegiant.co")
    if image:
        allowed = allowed or hostname == "sgliteasset.com" or hostname.endswith(".sgliteasset.com")
    if parsed.scheme != "https" or not allowed:
        target = "SiteGiant or SiteGiant asset" if image else "SiteGiant"
        raise ValueError(f"URL must use HTTPS on a {target} host")
    return value


class SiteGiantSkuItem(BaseModel):
    sku: str = Field(..., min_length=1, max_length=128)
    item_name: str = Field(..., min_length=1, max_length=500)
    source_item_id: Optional[str] = Field(None, max_length=64)
    source_item_url: Optional[str] = Field(None, max_length=512)
    image_url: Optional[str] = Field(None, max_length=1024)

    @field_validator("sku", mode="before")
    @classmethod
    def clean_sitegiant_sku(cls, value):
        return value.strip()

    @field_validator("item_name", mode="before")
    @classmethod
    def clean_item_name(cls, value):
        return value.strip()

    @field_validator("source_item_url")
    @classmethod
    def validate_source_url(cls, value):
        return _require_sitegiant_url(value)

    @field_validator("image_url")
    @classmethod
    def validate_image_url(cls, value):
        return _require_sitegiant_url(value, image=True)


class SiteGiantSkuSyncRequest(BaseModel):
    warehouse_id: int = Field(..., gt=0)
    sync_run_id: UUID
    captured_at: datetime
    page: int = Field(..., ge=1, le=10000)
    total_pages: int = Field(..., ge=1, le=10000)
    total_items: int = Field(..., ge=1, le=1000000)
    items: List[SiteGiantSkuItem] = Field(..., min_length=1, max_length=100)

    @field_validator("captured_at")
    @classmethod
    def require_timezone(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("captured_at must include a timezone offset")
        return value

    @model_validator(mode="after")
    def validate_page(self):
        if self.page > self.total_pages:
            raise ValueError("page must not exceed total_pages")
        return self


class CreateWorkSkuRequest(BaseModel):
    warehouse_id: int = Field(..., gt=0)
    sku: str = Field(..., min_length=1, max_length=128)
    item_name: str = Field(..., min_length=1, max_length=500)

    @field_validator("sku", mode="before")
    @classmethod
    def normalize_sku(cls, value):
        return value.strip().upper()

    @field_validator("item_name", mode="before")
    @classmethod
    def clean_item_name(cls, value):
        return value.strip()


class SiteGiantWorkloadSnapshotRequest(BaseModel):
    warehouse_id: int = Field(..., gt=0)
    captured_at: datetime
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    period_label: Optional[str] = Field(None, max_length=128)
    pending_packages: int = Field(..., ge=0, le=10000000)
    to_process_packages: int = Field(..., ge=0, le=10000000)
    printed_packages: int = Field(..., ge=0, le=10000000)
    pending_pickup_packages: int = Field(..., ge=0, le=10000000)
    dashboard_order_count: Optional[int] = Field(None, ge=0, le=10000000)
    source_url: str = Field("https://sitegiant.co/dashboard", max_length=512)
    idempotency_key: str = Field(..., min_length=8, max_length=128)

    @field_validator("captured_at")
    @classmethod
    def require_timezone(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("captured_at must include a timezone offset")
        return value

    @field_validator("source_url")
    @classmethod
    def require_sitegiant_dashboard(cls, value):
        if value.rstrip("/") != "https://sitegiant.co/dashboard":
            raise ValueError("source_url must be the SiteGiant dashboard")
        return value

    @model_validator(mode="after")
    def validate_period(self):
        if self.period_start and self.period_end and self.period_end < self.period_start:
            raise ValueError("period_end must not be earlier than period_start")
        return self
