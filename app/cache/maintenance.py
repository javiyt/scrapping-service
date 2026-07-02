"""Background cache maintenance service.

Runs periodic cleanup on the SQLite cache to prevent unbounded growth.
Started on FastAPI startup when ``cache.cleanup_enabled`` is ``True``.
"""

import asyncio
import logging

from app.cache.sqlite_cache import SqliteCache
from app.core.config import Settings
from app.metrics.prometheus import MetricsCollector, get_metrics

logger = logging.getLogger("scraper-api.cache.maintenance")


class CacheMaintenanceService:
    """Periodic background cache cleanup loop.

    Usage::

        service = CacheMaintenanceService(cache, settings)
        await service.start()   # starts the asyncio task
        ...
        await service.stop()    # cancels on shutdown
    """

    def __init__(
        self,
        cache: SqliteCache,
        settings: Settings,
    ) -> None:
        self._cache = cache
        self._settings = settings
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    # ------------------------------------------------------------------ public

    @property
    def is_running(self) -> bool:
        """Return ``True`` if the background loop is currently active."""
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Start the periodic cleanup loop.

        Logs a warning if the loop is already running (no duplicate).
        """
        if self.is_running:
            logger.warning("Cleanup loop is already running — ignoring duplicate start")
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "Cache cleanup loop started (interval=%ds)",
            self._settings.cache_cleanup_interval_seconds,
        )

    async def stop(self) -> None:
        """Stop the cleanup loop gracefully."""
        if self._task is None:
            return
        self._stopped.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info("Cache cleanup loop stopped")

    # --------------------------------------------------------------- internal

    async def _run_loop(self) -> None:
        """Main loop: run cleanup, then sleep for the configured interval."""
        metrics = get_metrics()
        while not self._stopped.is_set():
            try:
                result = self._cache.cleanup(
                    delete_expired_after_seconds=self._settings.cache_delete_expired_after_seconds,
                    max_entries=self._settings.cache_max_entries,
                    max_size_bytes=self._settings.cache_max_size_mb * 1024 * 1024,
                    vacuum=self._settings.cache_vacuum_after_cleanup,
                )
                logger.info("Cache cleanup completed: %s", result.model_dump_json())
                metrics.inc("cache_cleanup_runs_total")
                metrics.inc("cache_cleanup_deleted_entries_total", result.total_deleted)
                self._update_gauges(metrics, result)
            except Exception:
                logger.exception("Cache cleanup run failed")
                metrics.inc("cache_cleanup_errors_total")

            # Wait for the interval or a stop signal.
            try:
                await asyncio.wait_for(
                    self._stopped.wait(),
                    timeout=self._settings.cache_cleanup_interval_seconds,
                )
                break  # Stop signal received.
            except TimeoutError:
                continue  # Interval elapsed, run another cycle.

    @staticmethod
    def _update_gauges(metrics: MetricsCollector, result) -> None:
        """Update Prometheus-style gauge metrics after a cleanup run."""
        metrics.set_gauge("cache_size_bytes", result.size_after_bytes)
        metrics.set_gauge("cache_entries_total", result.entries_after)
        expired = result.entries_before - result.entries_after - result.deleted_expired
        expired = max(0, expired)
        metrics.set_gauge("cache_expired_entries_total", expired)
