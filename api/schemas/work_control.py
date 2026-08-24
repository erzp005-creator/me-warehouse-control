"""Validation models for ME Warehouse Control.

This module deliberately models execution facts only. It does not mutate the
canonical inventory, purchase-order or sales-order ledgers.
"""

from typing import List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


TASK_TYPES = ("PICKING", "PACKING", "RECEIVING", "PUTAWAY", "STOCK_CHECK", "OTHER")
TASK_ACTIONS = ("START", "PAUSE", "RESUME", "COMPLETE", "EXCEPTION", "CANCEL")
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
