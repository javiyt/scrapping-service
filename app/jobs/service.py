"""In-memory asynchronous job service for long-running scrape requests.

Jobs are processed in-order by a pool of background worker tasks.  The
service is **not durable** — all state is lost on service restart.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import secrets
from datetime import UTC, datetime
from typing import Any

from app.auth.resolver import get_profile_resolver
from app.cache.sqlite_cache import SqliteCache
from app.core.config import Settings
from app.core.errors import ScraperError
from app.jobs.models import Job, JobStatus
from app.metrics.prometheus import MetricsCollector
from app.scraper.response_processing import format_scrape_content, process_scrape_response
from app.scraper.service import ScraperService

logger = logging.getLogger("scraper-api.jobs")


def _new_job_id() -> str:
    """Generate a short unique job identifier."""
    return f"job_{secrets.token_urlsafe(16)}"


class JobService:
    """In-memory async job processor.

    When *scraper* is ``None``, the service builds per-job scraper
    instances using the job's stored effective settings overrides and
    the shared cache.  When *scraper* is provided (legacy mode), it
    uses that singleton scraper for all jobs.
    """

    def __init__(
        self,
        scraper: ScraperService | None,
        settings: Settings,
        cache: SqliteCache,
        metrics: MetricsCollector,
    ) -> None:
        self._scraper = scraper
        self._settings = settings
        self._cache = cache
        self._metrics = metrics

        # In-memory store: job_id → Job
        self._jobs: dict[str, Job] = {}

        # Pending jobs (holds job_id strings).
        self._queue: asyncio.Queue[str] = asyncio.Queue()

        # Background worker tasks.
        self._workers: list[asyncio.Task[None]] = []

        # Serialise state mutations.
        self._lock = asyncio.Lock()

        # Number of jobs currently being processed (for gauge).
        self._running_count = 0

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Launch the background worker pool."""
        concurrency = self._settings.jobs_max_concurrency
        logger.info(
            "Starting job worker pool (max_concurrency=%d)",
            concurrency,
        )
        for i in range(concurrency):
            worker = asyncio.create_task(
                self._worker_loop(i),
                name=f"job-worker-{i}",
            )
            self._workers.append(worker)

    async def stop(self) -> None:
        """Cancel all worker tasks and wait for them to finish."""
        logger.info("Stopping job worker pool (%d tasks)", len(self._workers))
        for w in self._workers:
            w.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        self._metrics.set_gauge("jobs_queue_size", 0)
        self._metrics.set_gauge("jobs_running", 0)

    # ------------------------------------------------------------------ crud

    async def create_job(
        self,
        url: str,
        scrape_config: dict[str, Any],
        *,
        extract_config: dict[str, Any] | None = None,
        normalize_config: dict[str, Any] | None = None,
        response_format: str | None = None,
        profile_name: str | None = None,
        effective_settings: Settings | None = None,
    ) -> Job:
        """Create a new queued job and enqueue it for processing.

        If the total number of stored jobs would exceed
        ``max_retained``, the oldest finished jobs are evicted first.

        Args:
            url: Target URL to scrape.
            scrape_config: Parameters for :meth:`ScraperService.scrape`.
            extract_config: Optional extraction config.
            normalize_config: Optional normalisation config.
            response_format: Optional v2 response format.
            profile_name: The authenticated profile name.
            effective_settings: The profile-effective settings snapshot.
                API keys are never stored in the job object.
        """
        async with self._lock:
            # Enforce retention limit: evict oldest finished jobs.
            self._evict_oldest_finished()

            # Extract a safe overrides snapshot from effective settings
            # (no secrets, no API keys).
            overrides_snapshot: dict[str, Any] | None = None
            if effective_settings is not None:
                overrides_snapshot = self._extract_overrides_snapshot(effective_settings)

            job = Job(
                job_id=_new_job_id(),
                status=JobStatus.QUEUED,
                url=url,
                config=scrape_config,
                extract_config=extract_config,
                normalize_config=normalize_config,
                response_format=response_format,
                profile_name=profile_name,
                effective_settings_overrides=overrides_snapshot,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            self._jobs[job.job_id] = job

        await self._queue.put(job.job_id)
        self._metrics.inc("jobs_created_total")
        self._metrics.set_gauge("jobs_queue_size", self._queue.qsize())
        auth = f" profile={profile_name}" if profile_name else ""
        logger.info("Job %s created for %s%s", job.job_id, url, auth)
        return job

    def get_job(self, job_id: str) -> Job | None:
        """Return the job with *job_id*, or ``None`` if not found."""
        return self._jobs.get(job_id)

    def list_jobs(self) -> list[Job]:
        """Return all known jobs (newest first)."""
        jobs = list(self._jobs.values())
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs

    async def cancel_job(self, job_id: str) -> Job | None:
        """Mark a queued job as cancelled.  No-op if already running or finished."""
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.status == JobStatus.QUEUED:
                job.status = JobStatus.CANCELLED
                job.updated_at = datetime.now(UTC)
                logger.info("Job %s cancelled", job_id)
                self._metrics.inc("jobs_cancelled_total")
        return job

    async def delete_job(self, job_id: str) -> bool:
        """Remove a job from the store entirely.  Returns ``True`` if found."""
        async with self._lock:
            if job_id in self._jobs:
                del self._jobs[job_id]
                logger.info("Job %s deleted", job_id)
                return True
        return False

    # ------------------------------------------------------------------ worker

    def _build_scraper_for_job(self, job: Job) -> ScraperService:
        """Build a :class:`ScraperService` with the job's effective settings.

        Falls back to the singleton scraper if no profile overrides are
        stored on the job.
        """
        if job.effective_settings_overrides is None:
            # No profile overrides — use the legacy singleton scraper.
            if self._scraper is not None:
                return self._scraper
            # Create one from global settings.
            return ScraperService(settings=self._settings, cache=self._cache)

        # Resolve effective settings from the stored profile name.
        try:
            resolver = get_profile_resolver()
            effective = resolver.effective_settings_for(job.profile_name)
        except RuntimeError:
            # Resolver not initialised — fall back to global settings.
            effective = self._settings

        return ScraperService(settings=effective, cache=self._cache)

    @staticmethod
    def _extract_overrides_snapshot(effective: Settings) -> dict[str, Any]:
        """Extract a safe snapshot of profile overrides from effective settings.

        This captures only the sections that profiles are allowed to
        override.  API keys are never included.
        """
        snapshot: dict[str, Any] = {}

        safe_sections = {
            "cache": (
                "default_ttl_seconds",
                "stale_if_error",
                "max_html_size_mb",
            ),
            "scraper": (
                "default_mode",
                "timeout_seconds",
                "max_concurrency",
                "headless",
                "user_agent_profile",
            ),
            "security": ("allowed_domains", "block_private_ips", "block_localhost"),
            "debug": ("screenshots", "html_dumps"),
            "browser": ("arguments",),
            "jobs": ("max_concurrency", "max_retained", "result_ttl_seconds"),
        }

        for section, fields in safe_sections.items():
            section_data: dict[str, Any] = {}
            for field in fields:
                full_name = f"{section}_{field}"
                if hasattr(effective, full_name):
                    section_data[field] = getattr(effective, full_name)
            if section_data:
                snapshot[section] = section_data

        # Include domain overrides if any.
        if effective.domains:
            snapshot["domains"] = copy.deepcopy(effective.domains)

        return snapshot

    async def _worker_loop(self, worker_index: int) -> None:
        """Main loop for a single background worker."""
        logger.debug("Worker %d started", worker_index)
        while True:
            try:
                job_id = await self._queue.get()
            except asyncio.CancelledError:
                logger.debug("Worker %d cancelled", worker_index)
                raise

            self._metrics.set_gauge("jobs_queue_size", self._queue.qsize())

            # Fetch the job — it may have been deleted or cancelled while queued.
            job = self._jobs.get(job_id)
            if job is None or job.status != JobStatus.QUEUED:
                self._queue.task_done()
                continue

            # ---- running
            async with self._lock:
                job.status = JobStatus.RUNNING
                job.started_at = datetime.now(UTC)
                job.updated_at = datetime.now(UTC)
            self._running_count += 1
            self._metrics.set_gauge("jobs_running", self._running_count)

            profile_tag = f" profile={job.profile_name}" if job.profile_name else ""
            logger.info("Job %s started (worker %d)%s", job_id, worker_index, profile_tag)

            # Build a scraper with job-specific effective settings.
            scraper = self._build_scraper_for_job(job)

            try:
                scrape_params = dict(job.config)

                result = await scraper.scrape(**scrape_params)
                result = process_scrape_response(
                    result,
                    normalize_config=job.normalize_config,
                    extract_config=job.extract_config,
                )
                if job.response_format is not None:
                    result = format_scrape_content(result, job.response_format)

                async with self._lock:
                    job.result = result
                    job.status = JobStatus.SUCCEEDED
                self._metrics.inc("jobs_succeeded_total")
                logger.info("Job %s succeeded%s", job_id, profile_tag)

            except ScraperError as exc:
                async with self._lock:
                    job.error = exc.to_dict()
                    job.status = JobStatus.FAILED
                self._metrics.inc("jobs_failed_total")
                logger.warning("Job %s failed: %s%s", job_id, exc.message, profile_tag)

            except asyncio.CancelledError:
                async with self._lock:
                    job.status = JobStatus.CANCELLED
                self._metrics.inc("jobs_cancelled_total")
                self._queue.task_done()
                raise

            except Exception as exc:
                async with self._lock:
                    job.error = {
                        "type": "internal_error",
                        "message": str(exc),
                        "details": {},
                    }
                    job.status = JobStatus.FAILED
                self._metrics.inc("jobs_failed_total")
                logger.exception("Job %s failed with unexpected error%s", job_id, profile_tag)

            finally:
                async with self._lock:
                    job.finished_at = datetime.now(UTC)
                    job.updated_at = datetime.now(UTC)
                self._running_count -= 1
                self._metrics.set_gauge("jobs_running", self._running_count)
                self._queue.task_done()

    # ------------------------------------------------------------------ retention

    def _evict_oldest_finished(self) -> None:
        """Remove oldest finished jobs when the store exceeds ``max_retained``.

        Only jobs in a terminal state (succeeded, failed, cancelled) are
        evicted.  Queued and running jobs are never removed by this policy.
        """
        max_retained = self._settings.jobs_max_retained
        if len(self._jobs) < max_retained:
            return

        # Gather all finished jobs, sorted oldest-first.
        finished: list[tuple[str, Job]] = [
            (jid, j)
            for jid, j in self._jobs.items()
            if j.status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED)
        ]
        finished.sort(key=lambda x: x[1].finished_at or x[1].created_at)

        # How many do we need to remove to stay under the limit?
        to_remove = len(self._jobs) - max_retained + 1  # +1 for the new job
        for jid, _ in finished[:to_remove]:
            del self._jobs[jid]
            logger.debug("Evicted finished job %s (retention limit)", jid)

        if to_remove > 0:
            logger.info(
                "Evicted %d finished job(s) to stay under max_retained=%d",
                min(to_remove, len(finished)),
                max_retained,
            )
