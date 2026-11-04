"""Simple Prometheus-style metrics collector.

Provides counters and histograms exposed via the ``/metrics`` endpoint.

Uses a lightweight in-process approach rather than the full
``prometheus-client`` library to keep the dependency footprint small.

Labelled counters (see :meth:`MetricsCollector.inc`) use a bounded,
operator-configured label set (currently provider names).  Query text,
result URLs and any other arbitrary user input must never appear in
labels, as they would create unbounded metric series.
"""

import threading
import time
from typing import Any


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
            "search_cache_hits_total": 0,
            "search_cache_misses_total": 0,
            "search_cache_stale_hits_total": 0,
            "search_cache_errors_total": 0,
            "search_requests_total": 0,
            "search_success_total": 0,
            "search_error_total": 0,
            "search_fallbacks_total": 0,
            "transcript_cache_hits_total": 0,
            "transcript_cache_misses_total": 0,
            "transcript_cache_stale_hits_total": 0,
            "transcript_cache_errors_total": 0,
            "transcript_fallbacks_total": 0,
            "transcript_requests_total": 0,
            "transcript_success_total": 0,
            "transcript_error_total": 0,
            "proxy_requests_total": 0,
            "proxy_errors_total": 0,
            "auth_requests_total": 0,
            "auth_failures_total": 0,
        }
        self._latencies: dict[str, float] = {
            "scrape_duration_ms_sum": 0.0,
            "search_duration_ms_sum": 0.0,
            "transcript_duration_ms_sum": 0.0,
        }
        # Labelled counters: metric name -> {((label, value), ...) -> count}.
        # Label keys are sorted so series render deterministically.
        self._labeled_counters: dict[str, dict[tuple[tuple[str, str], ...], int]] = {}
        self._gauges: dict[str, int | float] = {}
        self._up: bool = True
        self._consecutive_scrape_errors = 0
        self._last_scrape_error_at: float | None = None
        self._last_scrape_success_at: float | None = None

    # --------------------------------------------------------------- counters

    def inc(self, name: str, value: int = 1, **labels: str) -> None:
        """Increment a counter, optionally by a labelled series.

        With no labels the value is tracked as a plain counter; with labels
        it is tracked as a distinct Prometheus series.  Label values must
        come from a bounded set (e.g. configured provider names) — never
        from user input such as query text or URLs.
        """
        with self._lock:
            if labels:
                series = self._labeled_counters.setdefault(name, {})
                key = tuple(sorted(labels.items()))
                series[key] = series.get(key, 0) + value
            else:
                self._counters[name] = self._counters.get(name, 0) + value

    def get(self, name: str, **labels: str) -> int:
        """Return the current value of a counter (plain or labelled).

        A convenience accessor for tests and debug endpoints.  With labels,
        the labelled series is looked up; with no labels the plain counter is
        returned.  Missing series return 0.
        """
        with self._lock:
            if labels:
                series = self._labeled_counters.get(name, {})
                key = tuple(sorted(labels.items()))
                return series.get(key, 0)
            return self._counters.get(name, 0)

    def reset(self) -> None:
        """Reset all counters, gauges, and latencies to their initial values.

        Intended for tests and for the readiness probe that wants a clean
        slate.  Thread-safe.
        """
        with self._lock:
            for name in self._counters:
                self._counters[name] = 0
            for name in self._latencies:
                self._latencies[name] = 0.0
            self._labeled_counters.clear()
            self._gauges.clear()
            self._consecutive_scrape_errors = 0
            self._last_scrape_error_at = None
            self._last_scrape_success_at = None

    def mark_scrape_success(self) -> None:
        with self._lock:
            self._consecutive_scrape_errors = 0
            self._last_scrape_success_at = time.time()

    def mark_scrape_error(self) -> None:
        with self._lock:
            self._consecutive_scrape_errors += 1
            self._last_scrape_error_at = time.time()

    def health_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "consecutive_scrape_errors": self._consecutive_scrape_errors,
                "last_scrape_error_at": self._last_scrape_error_at,
                "last_scrape_success_at": self._last_scrape_success_at,
                "scrape_requests_total": self._counters.get("scrape_requests_total", 0),
                "scrape_success_total": self._counters.get("scrape_success_total", 0),
                "scrape_error_total": self._counters.get("scrape_error_total", 0),
            }

    def observe_latency(self, ms: float) -> None:
        with self._lock:
            self._latencies["scrape_duration_ms_sum"] += ms

    def observe_search_latency(self, ms: float) -> None:
        with self._lock:
            self._latencies["search_duration_ms_sum"] += ms

    def observe_transcript_latency(self, ms: float) -> None:
        with self._lock:
            self._latencies["transcript_duration_ms_sum"] += ms

    @staticmethod
    def _escape_label(value: str) -> str:
        """Escape a label value for the Prometheus text format."""
        return value.replace("\\", "\\\\").replace('"', '\\"')

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

            # Search result cache counters
            for counter_name, help_text in [
                ("search_cache_hits_total", "Search result cache hits"),
                ("search_cache_misses_total", "Search result cache misses"),
                (
                    "search_cache_stale_hits_total",
                    "Stale search cache entries served on provider error",
                ),
                (
                    "search_cache_errors_total",
                    "Search executions that failed (before any stale fallback)",
                ),
            ]:
                val = self._counters.get(counter_name, 0)
                lines.append(f"# HELP {counter_name} {help_text}")
                lines.append(f"# TYPE {counter_name} counter")
                lines.append(f"{counter_name} {val}")
                lines.append("")

            # Search request counters (API-level)
            for counter_name, help_text in [
                ("search_requests_total", "Total search requests received"),
                ("search_success_total", "Successful search responses"),
                ("search_error_total", "Failed search responses"),
            ]:
                val = self._counters.get(counter_name, 0)
                lines.append(f"# HELP {counter_name} {help_text}")
                lines.append(f"# TYPE {counter_name} counter")
                lines.append(f"{counter_name} {val}")
                lines.append("")

            lines.append("# HELP search_duration_ms Total search duration in ms (for avg)")
            lines.append("# TYPE search_duration_ms counter")
            lines.append(
                f"search_duration_ms_sum {self._latencies.get('search_duration_ms_sum', 0.0)}"
            )
            lines.append("")

            # Search fallback counter (API-level)
            for counter_name, help_text in [
                ("search_fallbacks_total", "Search requests that required a provider fallback"),
            ]:
                val = self._counters.get(counter_name, 0)
                lines.append(f"# HELP {counter_name} {help_text}")
                lines.append(f"# TYPE {counter_name} counter")
                lines.append(f"{counter_name} {val}")
                lines.append("")

            # Per-provider search counters.  The ``provider`` label is a
            # bounded, operator-configured set — never user input.
            for counter_name, help_text in [
                ("search_provider_attempts_total", "Search attempts made, by provider"),
                (
                    "search_provider_success_total",
                    "Successful search responses, by provider",
                ),
                ("search_provider_error_total", "Failed search responses, by provider"),
            ]:
                lines.append(f"# HELP {counter_name} {help_text}")
                lines.append(f"# TYPE {counter_name} counter")
                for labels, val in sorted(self._labeled_counters.get(counter_name, {}).items()):
                    label_part = ",".join(
                        f'"{key}"="{self._escape_label(value)}"' for key, value in labels
                    )
                    lines.append(f"{counter_name}{{{label_part}}} {val}")
                lines.append("")

            lines.append("")

            # Transcript result cache counters
            for counter_name, help_text in [
                ("transcript_cache_hits_total", "Transcript result cache hits"),
                ("transcript_cache_misses_total", "Transcript result cache misses"),
                (
                    "transcript_cache_stale_hits_total",
                    "Stale transcript cache entries served on provider error",
                ),
                (
                    "transcript_cache_errors_total",
                    "Transcript cache read/write errors (degraded to live/uncached)",
                ),
            ]:
                val = self._counters.get(counter_name, 0)
                lines.append(f"# HELP {counter_name} {help_text}")
                lines.append(f"# TYPE {counter_name} counter")
                lines.append(f"{counter_name} {val}")
                lines.append("")

            # Transcript router fallback counter (API-level)
            val = self._counters.get("transcript_fallbacks_total", 0)
            lines.append(
                "# HELP transcript_fallbacks_total "
                "Transcript requests that required a provider fallback"
            )
            lines.append("# TYPE transcript_fallbacks_total counter")
            lines.append(f"transcript_fallbacks_total {val}")
            lines.append("")

            # Transcript error counter (API-level)
            val = self._counters.get("transcript_error_total", 0)
            lines.append("# HELP transcript_error_total Failed transcript responses")
            lines.append("# TYPE transcript_error_total counter")
            lines.append(f"transcript_error_total {val}")
            lines.append("")

            # Per-provider transcript counters.  The ``provider`` label is a
            # bounded, operator-configured set — never user input.
            for counter_name, help_text in [
                ("transcript_provider_attempts_total", "Transcript attempts made, by provider"),
                (
                    "transcript_provider_success_total",
                    "Successful transcript responses, by provider",
                ),
                ("transcript_provider_error_total", "Failed transcript responses, by provider"),
            ]:
                lines.append(f"# HELP {counter_name} {help_text}")
                lines.append(f"# TYPE {counter_name} counter")
                for labels, val in sorted(self._labeled_counters.get(counter_name, {}).items()):
                    label_part = ",".join(
                        f'"{key}"="{self._escape_label(value)}"' for key, value in labels
                    )
                    lines.append(f"{counter_name}{{{label_part}}} {val}")
                lines.append("")

            # Transcript latency
            lines.append("# HELP transcript_duration_ms Total transcript duration in ms (for avg)")
            lines.append("# TYPE transcript_duration_ms counter")
            lines.append(
                f"transcript_duration_ms_sum "
                f"{self._latencies.get('transcript_duration_ms_sum', 0.0)}"
            )
            lines.append("")

            # Auth counters
            for counter_name, help_text in [
                ("auth_requests_total", "Total authenticated requests"),
                ("auth_failures_total", "Total authentication failures"),
            ]:
                val = self._counters.get(counter_name, 0)
                lines.append(f"# HELP {counter_name} {help_text}")
                lines.append(f"# TYPE {counter_name} counter")
                lines.append(f"{counter_name} {val}")
                lines.append("")

            # Per-browser-backend counters.  The ``backend`` label is a
            # bounded, operator-configured set of backend names (e.g.
            # ``botasaurus``, ``camofox``) — never a URL or other user input.
            for counter_name, help_text in [
                ("browser_backend_attempt_total", "Browser fetch attempts made, by backend"),
                (
                    "browser_backend_success_total",
                    "Successful browser fetches, by backend",
                ),
                ("browser_backend_error_total", "Failed browser fetches, by backend"),
                (
                    "browser_backend_fallback_total",
                    "Browser fetches that required a fallback backend",
                ),
            ]:
                lines.append(f"# HELP {counter_name} {help_text}")
                lines.append(f"# TYPE {counter_name} counter")
                for labels, val in sorted(self._labeled_counters.get(counter_name, {}).items()):
                    label_part = ",".join(
                        f'"{key}"="{self._escape_label(value)}"' for key, value in labels
                    )
                    lines.append(f"{counter_name}{{{label_part}}} {val}")
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
                "search_cache_entries_total",
                "search_cache_size_bytes",
                "transcript_cache_entries_total",
                "transcript_cache_size_bytes",
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
