"""Health-check schemas."""

from typing import Any

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Response for the ``/health`` endpoint."""

    status: str = Field(default="ok", pattern=r"^(ok|degraded|unavailable)$")
    version: str = "1.0.0"
    service: str = "scraper-api"


class ReadinessResponse(BaseModel):
    """Response for the ``/ready`` endpoint."""

    status: str = Field(default="ok", pattern=r"^(ok|degraded|unavailable)$")
    checks: dict[str, Any] = Field(default_factory=dict)
