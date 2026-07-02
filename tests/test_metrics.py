"""Tests for the Prometheus-style metrics collector."""

from app.metrics.prometheus import MetricsCollector, get_metrics


class TestMetricsCollector:
    def test_counter_inc(self):
        m = MetricsCollector()
        m.inc("scrape_requests_total")
        m.inc("scrape_requests_total")
        assert "scrape_requests_total 2" in m.render()

    def test_counter_inc_by_value(self):
        m = MetricsCollector()
        m.inc("scrape_requests_total", 5)
        assert "scrape_requests_total 5" in m.render()

    def test_counter_inc_stores_value(self):
        m = MetricsCollector()
        m.inc("scrape_requests_total", 3)
        output = m.render()
        assert "scrape_requests_total 3" in output

    def test_observe_latency(self):
        m = MetricsCollector()
        m.observe_latency(150.5)
        assert "scrape_duration_ms_sum 150.5" in m.render()

    def test_set_gauge(self):
        m = MetricsCollector()
        m.set_gauge("jobs_running", 3)
        assert "jobs_running 3" in m.render()

    def test_set_up(self):
        m = MetricsCollector()
        m.set_up(True)
        assert "scraper_up 1" in m.render()
        m.set_up(False)
        assert "scraper_up 0" in m.render()

    def test_render_includes_cache_cleanup_counters(self):
        m = MetricsCollector()
        output = m.render()
        assert "# HELP cache_cleanup_runs_total" in output
        assert "# HELP cache_cleanup_deleted_entries_total" in output
        assert "# HELP cache_cleanup_errors_total" in output
        assert "# HELP cache_vacuum_runs_total" in output
        assert "# HELP cache_vacuum_errors_total" in output

    def test_render_includes_cache_gauges(self):
        m = MetricsCollector()
        m.set_gauge("cache_size_bytes", 1024)
        m.set_gauge("cache_entries_total", 42)
        m.set_gauge("cache_expired_entries_total", 5)
        output = m.render()
        assert "cache_size_bytes 1024" in output
        assert "cache_entries_total 42" in output
        assert "cache_expired_entries_total 5" in output

    def test_render_includes_proxy_counters(self):
        m = MetricsCollector()
        m.inc("proxy_requests_total", 3)
        m.inc("proxy_errors_total", 1)
        output = m.render()
        assert "proxy_requests_total 3" in output
        assert "proxy_errors_total 1" in output

    def test_render_includes_job_gauges(self):
        m = MetricsCollector()
        m.set_gauge("jobs_queue_size", 0)
        output = m.render()
        assert "jobs_queue_size 0" in output


class TestGetMetrics:
    def test_singleton(self):
        a = get_metrics()
        b = get_metrics()
        assert a is b
