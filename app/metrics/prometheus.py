"""Simple Prometheus-style metrics collector.

Provides counters and histograms exposed via the ``/metrics`` endpoint.

Uses a lightweight in-process approach rather than the full
``prometheus-client`` library to keep the dependency footprint small.
"""

import threading


class MetricsCollector:
    """Thread-safe metrics collector.

    Exposes counters, gauges, and a simple text representation suitable
    for Prometheus scraping.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {
            "scrape_requests_total": 0,
            "scrape_success_total": 0,
            "scrape_error_total": 0,
            "extraction_requests_total": 0,
            "extraction_success_total": 0,
            "extraction_error_total": 0,
            "jobs_created_total": 0,
            "jobs_succeeded_total": 0,
            "jobs_failed_total": 0,
            "jobs_cancelled_total": 0,
            "cache_hits_total": 0,
            "cache_misses_total": 0,
            "cache_stale_hits_total": 0,
            "proxy_requests_total": 0,
            "proxy_errors_total": 0,
        }
        self._latencies: dict[str, float] = {
            "scrape_duration_ms_sum": 0.0,
        }
        self._gauges: dict[str, int | float] = {}
        self._up: bool = True

    # --------------------------------------------------------------- counters

    def inc(self, name: str, value: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + value

    def observe_latency(self, ms: float) -> None:
        with self._lock:
            self._latencies["scrape_duration_ms_sum"] += ms

    def set_up(self, up: bool) -> None:
        with self._lock:
            self._up = up

    # ---------------------------------------------------------------- gauges

    def set_gauge(self, name: str, value: int | float) -> None:
        """Set a gauge metric to an absolute value."""
        with self._lock:
            self._gauges[name] = value

    # --------------------------------------------------------------- render

    def render(self) -> str:
        """Return Prometheus text-format metrics."""
        with self._lock:
            lines: list[str] = [
                "# HELP scraper_up Service health (1 = up, 0 = down)",
                "# TYPE scraper_up gauge",
                f"scraper_up {1 if self._up else 0}",
                "",
                "# HELP scrape_requests_total Total scrape requests received",
                "# TYPE scrape_requests_total counter",
                f"scrape_requests_total {self._counters.get('scrape_requests_total', 0)}",
                "",
                "# HELP scrape_success_total Successful scrape responses",
                "# TYPE scrape_success_total counter",
                f"scrape_success_total {self._counters.get('scrape_success_total', 0)}",
                "",
                "# HELP scrape_error_total Failed scrape responses",
                "# TYPE scrape_error_total counter",
                f"scrape_error_total {self._counters.get('scrape_error_total', 0)}",
                "",
                "# HELP cache_hits_total Cache hits",
                "# TYPE cache_hits_total counter",
                f"cache_hits_total {self._counters.get('cache_hits_total', 0)}",
                "",
                "# HELP cache_misses_total Cache misses",
                "# TYPE cache_misses_total counter",
                f"cache_misses_total {self._counters.get('cache_misses_total', 0)}",
                "",
                "# HELP cache_stale_hits_total Stale cache served on error",
                "# TYPE cache_stale_hits_total counter",
                f"cache_stale_hits_total {self._counters.get('cache_stale_hits_total', 0)}",
                "",
                "# HELP extraction_requests_total Total extraction requests",
                "# TYPE extraction_requests_total counter",
                f"extraction_requests_total {self._counters.get('extraction_requests_total', 0)}",
                "",
                "# HELP extraction_success_total Successful extraction responses",
                "# TYPE extraction_success_total counter",
                f"extraction_success_total {self._counters.get('extraction_success_total', 0)}",
                "",
                "# HELP extraction_error_total Failed extraction responses",
                "# TYPE extraction_error_total counter",
                f"extraction_error_total {self._counters.get('extraction_error_total', 0)}",
                "",
                "# HELP jobs_created_total Total async jobs created",
                "# TYPE jobs_created_total counter",
                f"jobs_created_total {self._counters.get('jobs_created_total', 0)}",
                "",
                "# HELP jobs_running Currently running async jobs",
                "# TYPE jobs_running gauge",
                f"jobs_running {self._gauges.get('jobs_running', 0)}",
                "",
                "# HELP jobs_succeeded_total Successful async jobs",
                "# TYPE jobs_succeeded_total counter",
                f"jobs_succeeded_total {self._counters.get('jobs_succeeded_total', 0)}",
                "",
                "# HELP jobs_failed_total Failed async jobs",
                "# TYPE jobs_failed_total counter",
                f"jobs_failed_total {self._counters.get('jobs_failed_total', 0)}",
                "",
                "# HELP jobs_cancelled_total Cancelled async jobs",
                "# TYPE jobs_cancelled_total counter",
                f"jobs_cancelled_total {self._counters.get('jobs_cancelled_total', 0)}",
                "",
                "# HELP jobs_queue_size Number of jobs waiting in queue",
                "# TYPE jobs_queue_size gauge",
                f"jobs_queue_size {self._gauges.get('jobs_queue_size', 0)}",
                "",
                "# HELP scrape_duration_ms Total scrape duration in ms (for avg)",
                "# TYPE scrape_duration_ms counter",
                f"scrape_duration_ms_sum {self._latencies.get('scrape_duration_ms_sum', 0.0)}",
                "",
            ]

            # Cache cleanup counters
            for counter_name, help_text, suffix in [
                ("cache_cleanup_runs_total", "Total cache cleanup runs", ""),
                (
                    "cache_cleanup_deleted_entries_total",
                    "Total cache entries deleted by cleanup",
                    "",
                ),
                ("cache_cleanup_errors_total", "Total cache cleanup errors", ""),
                ("cache_vacuum_runs_total", "Total cache VACUUM runs", ""),
                ("cache_vacuum_errors_total", "Total cache VACUUM errors", ""),
            ]:
                val = self._counters.get(counter_name, 0)
                lines.append(f"# HELP {counter_name} {help_text}")
                lines.append(f"# TYPE {counter_name} counter")
                lines.append(f"{counter_name}{suffix} {val}")
                lines.append("")

            # Proxy counters
            for counter_name, help_text in [
                ("proxy_requests_total", "Total scrape requests routed through a proxy"),
                ("proxy_errors_total", "Total proxy-related errors"),
            ]:
                val = self._counters.get(counter_name, 0)
                lines.append(f"# HELP {counter_name} {help_text}")
                lines.append(f"# TYPE {counter_name} counter")
                lines.append(f"{counter_name} {val}")
                lines.append("")

            # Gauges
            for gauge_name in [
                "cache_size_bytes",
                "cache_entries_total",
                "cache_expired_entries_total",
            ]:
                val = self._gauges.get(gauge_name, 0)
                lines.append(f"# HELP {gauge_name} {gauge_name.replace('_', ' ')}")
                lines.append(f"# TYPE {gauge_name} gauge")
                lines.append(f"{gauge_name} {val}")
                lines.append("")

        return "\n".join(lines)


# Module-level singleton.
_metrics: MetricsCollector | None = None


def get_metrics() -> MetricsCollector:
    global _metrics
    if _metrics is None:
        _metrics = MetricsCollector()
    return _metrics
