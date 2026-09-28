"""Pydantic schemas for the REST API layer.

Kept separate from `ping/models.py` (the engine's internal dataclasses)
on purpose: the API contract and the engine's internal representation
are allowed to evolve independently.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator


# --- Ping (on-demand) ----------------------------------------------------

class PingRequest(BaseModel):
    host: str = Field(..., min_length=1, max_length=253)
    count: int = Field(4, ge=1, le=20)
    timeout: float = Field(1.0, gt=0, le=10)
    interval: float = Field(0.2, ge=0, le=5)

    @field_validator("host")
    @classmethod
    def strip_host(cls, v: str) -> str:
        return v.strip()


class LatencyOut(BaseModel):
    min: Optional[float] = None
    avg: Optional[float] = None
    max: Optional[float] = None


class ReplyOut(BaseModel):
    sequence: int
    success: bool
    latency_ms: Optional[float] = None


class PingResponse(BaseModel):
    host: str
    resolved_ip: Optional[str]
    status: str
    sent: int
    received: int
    packet_loss: float
    latency: LatencyOut
    replies: list[ReplyOut]
    timestamp: str
    error: Optional[str] = None
    previous_status: Optional[str] = None
    status_changed: Optional[bool] = None


# --- Targets ---------------------------------------------------------------

class TargetCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    host: str = Field(..., min_length=1, max_length=253)
    interval: int = Field(10, ge=1, le=86400)
    count: int = Field(4, ge=1, le=20)
    timeout: float = Field(1.0, gt=0, le=10)
    enabled: bool = True
    
    telegram_enabled: bool = False
    telegram_name: Optional[str] = None
    telegram_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    telegram_notify_down: bool = True
    telegram_notify_up: bool = True
    telegram_custom: bool = False
    telegram_template_down: Optional[str] = None
    telegram_template_up: Optional[str] = None

    @field_validator("name", "host")
    @classmethod
    def strip_strings(cls, v: str) -> str:
        return v.strip()


class TargetUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    host: Optional[str] = Field(None, min_length=1, max_length=253)
    interval: Optional[int] = Field(None, ge=1, le=86400)
    count: Optional[int] = Field(None, ge=1, le=20)
    timeout: Optional[float] = Field(None, gt=0, le=10)
    enabled: Optional[bool] = None
    
    telegram_enabled: Optional[bool] = None
    telegram_name: Optional[str] = None
    telegram_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    telegram_notify_down: Optional[bool] = None
    telegram_notify_up: Optional[bool] = None
    telegram_custom: Optional[bool] = None
    telegram_template_down: Optional[str] = None
    telegram_template_up: Optional[str] = None


class TargetOut(BaseModel):
    id: int
    name: str
    host: str
    interval: int
    count: int
    timeout: float
    enabled: bool
    telegram_enabled: bool
    telegram_name: Optional[str]
    telegram_token: Optional[str]
    telegram_chat_id: Optional[str]
    telegram_notify_down: bool
    telegram_notify_up: bool
    telegram_custom: bool
    telegram_template_down: Optional[str]
    telegram_template_up: Optional[str]
    created_at: str


class TargetStateOut(TargetOut):
    """Target combined with its live monitoring state, used by the
    dashboard and target list so the frontend never needs a second
    round trip per row."""

    raw_status: str = "UNKNOWN"
    status: str = "UNKNOWN"  # effective_status
    latency_min: Optional[float] = None
    latency_avg: Optional[float] = None
    latency_max: Optional[float] = None
    packet_loss: Optional[float] = None
    last_check: Optional[str] = None
    status_changed: bool = False


class HistoryPointOut(BaseModel):
    timestamp: str
    status: str
    latency_avg: Optional[float] = None
    packet_loss: float = 0.0

class TargetSummaryOut(BaseModel):
    availability: Optional[float]
    uptime: int
    downtime: int
    incidents: int

class TargetHistoryResponse(BaseModel):
    summary: TargetSummaryOut
    heartbeat: list[HistoryPointOut]


# --- Dashboard ---------------------------------------------------------

class DashboardResponse(BaseModel):
    total: int
    up: int
    degraded: int
    down: int
    unknown: int
    paused: int = 0
    last_update: Optional[str] = None
    targets: list[TargetStateOut]


class HealthResponse(BaseModel):
    status: str

class TelegramTestRequest(BaseModel):
    token: str
    chat_id: str
    name: Optional[str] = "PingOn"


# --- Auth & Users --------------------------------------------------------

class UserBase(BaseModel):
    username: str = Field(..., min_length=3)
    role: str = "guest"
    is_active: bool = True
    must_change_password: bool = False
    
    @field_validator("username")
    @classmethod
    def clean_username(cls, v: str) -> str:
        return v.strip().lower()
        
    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        v = v.lower()
        if v not in ("administrator", "guest"):
            raise ValueError("Role must be administrator or guest")
        return v

class UserCreate(UserBase):
    password: str = Field(..., min_length=8)

class UserUpdate(BaseModel):
    username: Optional[str] = Field(None, min_length=3)
    password: Optional[str] = Field(None, min_length=8)
    role: Optional[str] = None
    is_active: Optional[bool] = None
    must_change_password: Optional[bool] = None

    @field_validator("username")
    @classmethod
    def clean_username(cls, v: Optional[str]) -> Optional[str]:
        return v.strip().lower() if v else v
        
    @field_validator("role")
    @classmethod
    def validate_role(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            v = v.lower()
            if v not in ("administrator", "guest"):
                raise ValueError("Role must be administrator or guest")
        return v

class UserOut(UserBase):
    id: int
    created_at: str
    updated_at: str

class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=8)

class Token(BaseModel):
    access_token: str
    token_type: str
    user: UserOut
