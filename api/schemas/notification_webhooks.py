"""Request schemas for /api/admin/notification-webhooks.

The table backs per-warehouse Teams (initially) notification
destinations for the backorder.* event family. URLs are
Fernet-encrypted at rest via SENTRY_ENCRYPTION_KEY; the API never
echoes the plaintext URL back, only a masked host preview.
"""

from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


SUPPORTED_CHANNELS = ("teams",)

DEFAULT_EVENT_FILTER = ["backorder.opened", "backorder.fulfillable"]

# Mirrors the registered backorder.* set in
# api/services/events_schema_registry.py. Kept inline so a typo in the
# admin UI is rejected at the Pydantic layer rather than at emit time.
ALLOWED_EVENT_TYPES = (
    "backorder.opened",
    "backorder.fulfillable",
    "backorder.cancelled",
)


class CreateNotificationWebhookRequest(BaseModel):
    warehouse_id: int = Field(..., gt=0)
    channel_kind: str = Field(..., min_length=1, max_length=20)
    url:          str = Field(..., min_length=1, max_length=2048)
    event_filter: Optional[List[str]] = Field(default=None)
    enabled:      bool = Field(default=True)
    secret:       Optional[str] = Field(default=None, max_length=128)

    @field_validator("channel_kind")
    @classmethod
    def _kind_must_be_supported(cls, v: str) -> str:
        if v not in SUPPORTED_CHANNELS:
            raise ValueError(
                f"channel_kind must be one of {SUPPORTED_CHANNELS}; got {v!r}"
            )
        return v

    @field_validator("event_filter")
    @classmethod
    def _filter_values_must_be_known(cls, v):
        if v is None:
            return v
        for entry in v:
            if entry not in ALLOWED_EVENT_TYPES:
                raise ValueError(
                    f"event_filter value {entry!r} is not one of {ALLOWED_EVENT_TYPES}"
                )
        return v


class UpdateNotificationWebhookRequest(BaseModel):
    """Patches the enabled flag, event filter, or secret. The URL is
    write-only after create; use /rotate-url for that path so a
    rotation looks distinct in the audit trail."""
    enabled:      Optional[bool] = None
    event_filter: Optional[List[str]] = None
    secret:       Optional[str] = Field(default=None, max_length=128)

    @field_validator("event_filter")
    @classmethod
    def _filter_values_must_be_known(cls, v):
        if v is None:
            return v
        for entry in v:
            if entry not in ALLOWED_EVENT_TYPES:
                raise ValueError(
                    f"event_filter value {entry!r} is not one of {ALLOWED_EVENT_TYPES}"
                )
        return v


class RotateNotificationWebhookUrlRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=2048)
