"""Simple Prometheus-style metrics collector.

Provides counters and histograms exposed via the ``/metrics`` endpoint.

Uses a lightweight in-process approach rather than the full
``prometheus-client`` library to keep the dependency footprint small.
"""

import threading


class MetricsCollector:
    """Thread-safe metrics collector.

    Exposes counters and a simple text representation suitable for Prometheus
    scraping.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {
            "scrape_requests_total": 0,
            "scrape_success_total": 0,
            "scrape_error_total": 0,
            "cache_hits_total": 0,
            "cache_misses_total": 0,
            "cache_stale_hits_total": 0,
        }
        self._latencies: dict[str, float] = {
            "scrape_duration_ms_sum": 0.0,
        }
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
                "# HELP scrape_duration_ms Total scrape duration in ms (for avg)",
                "# TYPE scrape_duration_ms counter",
                f"scrape_duration_ms_sum {self._latencies.get('scrape_duration_ms_sum', 0.0)}",
                "",
            ]
        return "\n".join(lines)


# Module-level singleton.
_metrics: MetricsCollector | None = None


def get_metrics() -> MetricsCollector:
    global _metrics
    if _metrics is None:
        _metrics = MetricsCollector()
    return _metrics
