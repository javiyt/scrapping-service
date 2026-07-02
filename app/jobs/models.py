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


class JobListResponse(BaseModel):
    """Response wrapper for the job list endpoint."""

    jobs: list[JobResponse]
    total: int
