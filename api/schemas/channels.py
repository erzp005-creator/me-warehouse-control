"""Pydantic schemas for /api/admin/channels (v1.30.0, Pipe C).

Strict-typed bodies for the channel-availability config CRUD. ``extra='forbid'``
so an unknown field surfaces as a 400 rather than slipping past and dropping at
the SQL projection. Mirrors the v1.6 webhook-subscription schema shape.
"""

import re
from typing import Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Transform constants are injected verbatim into the outbound payload, so keep
# them to JSON scalars -- no nested objects to smuggle structure through.
_Scalar = Union[str, int, float, bool]

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class ChannelScope(BaseModel):
    """Which items publish on a channel and which warehouses count toward the
    number. An absent / empty dimension imposes no restriction on that
    dimension."""

    model_config = ConfigDict(extra="forbid")

    skus: Optional[List[str]] = None
    categories: Optional[List[str]] = None
    warehouse_ids: Optional[List[int]] = None


class ChannelTransform(BaseModel):
    """Declarative outbound transform: rename payload keys, then inject
    constants. No expression evaluation."""

    model_config = ConfigDict(extra="forbid")

    rename: Optional[Dict[str, str]] = None
    constants: Optional[Dict[str, _Scalar]] = None


class CreateChannelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: str = Field(..., min_length=1, max_length=64)
    display_name: str = Field(..., min_length=1, max_length=128)
    delivery_url: str = Field(..., min_length=1, max_length=2048)
    sku_scope: ChannelScope = Field(default_factory=ChannelScope)
    transform: ChannelTransform = Field(default_factory=ChannelTransform)
    rate_limit_per_second: int = Field(default=10, ge=1, le=1000)
    batch_size: int = Field(default=100, ge=1, le=1000)
    debounce_seconds: int = Field(default=30, ge=0, le=3600)
    pending_ceiling: int = Field(default=100000, ge=100, le=1000000)
    dlq_ceiling: int = Field(default=1000, ge=10, le=100000)

    @field_validator("channel_id")
    @classmethod
    def _slug(cls, v: str) -> str:
        v = v.strip()
        if not _SLUG_RE.match(v):
            raise ValueError(
                "channel_id must be a lowercase slug: start alphanumeric, then "
                "a-z, 0-9, hyphen or underscore"
            )
        return v

    @field_validator("delivery_url")
    @classmethod
    def _strip_url(cls, v: str) -> str:
        return v.strip()


class UpdateChannelRequest(BaseModel):
    """Body for PATCH /api/admin/channels/<id>. All fields optional; an absent
    field leaves the column unchanged. ``status`` accepts only the operator
    transitions 'active' / 'paused' -- the 'revoked' terminal status is reached
    via DELETE, so a typo cannot revoke a channel."""

    model_config = ConfigDict(extra="forbid")

    display_name: Optional[str] = Field(None, min_length=1, max_length=128)
    delivery_url: Optional[str] = Field(None, min_length=1, max_length=2048)
    sku_scope: Optional[ChannelScope] = None
    transform: Optional[ChannelTransform] = None
    rate_limit_per_second: Optional[int] = Field(None, ge=1, le=1000)
    batch_size: Optional[int] = Field(None, ge=1, le=1000)
    debounce_seconds: Optional[int] = Field(None, ge=0, le=3600)
    pending_ceiling: Optional[int] = Field(None, ge=100, le=1000000)
    dlq_ceiling: Optional[int] = Field(None, ge=10, le=100000)
    status: Optional[str] = None

    @field_validator("delivery_url")
    @classmethod
    def _strip_url(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() if v is not None else v

    @field_validator("status")
    @classmethod
    def _status_only_active_or_paused(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if v not in ("active", "paused"):
            raise ValueError(
                "status accepts only 'active' or 'paused'. Use the DELETE "
                "endpoint to revoke a channel."
            )
        return v
