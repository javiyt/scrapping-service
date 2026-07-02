"""Data models for async scrape jobs."""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


class JobStatus(enum.StrEnum):
    """Lifecycle states for an async scrape job."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Job(BaseModel):
    """Internal representation of an async scrape job.

    Jobs are held in-memory only — they are **not** durable across service
    restarts.  The ``config`` field stores the parameters that the worker
    will pass to :meth:`ScraperService.scrape`.

    Each job carries a ``profile_name`` so that the worker can resolve
    the correct effective settings at execution time.  A safe snapshot
    of effective settings is stored separately — API keys are never
    included.
    """

    job_id: str
    status: JobStatus = JobStatus.QUEUED
    url: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    extract_config: dict[str, Any] | None = None
    normalize_config: dict[str, Any] | None = None
    profile_name: str | None = None
    # A safe snapshot of effective settings overrides (no API keys).
    # This is a dict of section -> overrides, e.g. {"scraper": {"default_mode": "browser"}}
    effective_settings_overrides: dict[str, Any] | None = None


class JobResponse(BaseModel):
    """Public response model for a job — includes all fields except config."""

    job_id: str
    status: JobStatus
    url: str
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    profile_name: str | None = Field(
        default=None,
        description="Authenticated profile name (only shown when "
        "``auth.expose_profile_in_response`` is ``True``).",
    )


class JobListResponse(BaseModel):
    """Response wrapper for the job list endpoint."""

    jobs: list[JobResponse]
    total: int
