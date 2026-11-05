"""MCP adapter metrics and structured tracing (ticket #153).

Metrics are **additive**: the existing ``/metrics`` text output keeps its
exact shape.  This module renders its own counters, and the adapter's
``/metrics`` convenience endpoint (stdio entry point) serves a document
with two sections — the service's native metrics (fetched from the
service) and the adapter's MCP counters — so a Prometheus scrape of the
adapter covers both.

Adapter metrics:

- ``scraper_mcp_tool_calls_total{tool, result}`` — one series per tool and
  outcome (``ok`` or the mapped error type).  Labels come from a bounded
  set: the tool name is one of ~15 fixed names and the result is a fixed
  enum; never user input.
- ``scraper_mcp_tool_duration_seconds_total{tool}`` /
  ``_count{tool}`` — a Prometheus histogram is overkill for a stdio helper;
  sum + count give an average with zero SDK dependency.
- ``scraper_mcp_session_errors_total{code}`` — transport/session failures.

Structured logs — one JSON line per tool call on logger ``scraper-api.mcp``
carrying ``request_id``, ``tool``, ``result``, ``duration_ms`` and, when a
failure originated in the *service*, the service's own request id
(``x-request-id`` response header) so the two traces correlate.  Log lines
never contain the API key.

The service's ``MetricsCollector`` is never touched: the adapter keeps its
own private registry so the shared library stays free of MCP concepts.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

logger = logging.getLogger("scraper-api.mcp")


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class MetricsRegistry:
    """A minimal, thread-safe labelled-metrics registry (adapter-private)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, dict[tuple[tuple[str, str], ...], int]] = {}

    def inc(self, name: str, value: int = 1, **labels: str) -> None:
        with self._lock:
            series = self._counters.setdefault(name, {})
            key = tuple(sorted(labels.items()))
            series[key] = series.get(key, 0) + value

    def set_gauge(self, name: str, value: float, **labels: str) -> None:
        with self._lock:
            series = self._counters.setdefault(name, {})
            key = tuple(sorted(labels.items()))
            series[key] = value

    def render(self) -> str:
        """Prometheus text format for every recorded series (deterministic)."""
        lines: list[str] = []
        with self._lock:
            for name in sorted(self._counters):
                for labels, value in sorted(self._counters[name].items()):
                    label_part = ",".join(
                        f'"{key}"="{str(val).replace(chr(34), chr(92) + chr(34))}"'
                        for key, val in labels
                    )
                    kind = "gauge" if name.endswith("_seconds_total") else "counter"
                    if not lines or lines[-1] != f"# TYPE {name} {kind}":
                        lines.append(f"# TYPE {name} {kind}")
                    lines.append(f"{name}{{{label_part}}} {value}")
        return "\n".join(lines)


class ToolCallContext:
    """Accumulates one tool call's tracing state (request id, timing)."""

    def __init__(self, tool: str, request_id: str, profile_id: str | None = None) -> None:
        self.tool = tool
        self.request_id = request_id
        self.profile_id = profile_id
        self.started_at = time.monotonic()
        self.service_request_id: str | None = None

    def log_end(self, result: str, *, status: int | None = None) -> None:
        """Emit the structured JSON log line for this call (best-effort)."""
        entry: dict[str, Any] = {
            "event": "mcp_tool_call",
            "request_id": self.request_id,
            "tool": self.tool,
            "profile_id": self.profile_id,
            "result": result,
            "duration_ms": int((time.monotonic() - self.started_at) * 1000),
        }
        if status is not None:
            entry["status"] = status
        if self.service_request_id:
            entry["service_request_id"] = self.service_request_id
        try:
            logger.info("mcp_tool_call %s", _json(entry))
        except Exception:  # noqa: BLE001 — tracing must never break a call
            pass


class MCPMetrics:
    """Thread-safe adapter metrics and a Prometheus rendering of them.

    Every method is best-effort and exception-free: observability must
    never fail or delay a tool call.
    """

    def __init__(self) -> None:
        self._registry = MetricsRegistry()
        self._calls_total = 0
        self._last_call_at: float | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------- recording
    def record_tool_call(self, tool: str, result: str, duration_seconds: float) -> None:
        with self._lock:
            self._calls_total += 1
            self._last_call_at = time.time()
        self._registry.inc("scraper_mcp_tool_calls_total", tool=tool, result=result)
        self._registry.set_gauge(
            "scraper_mcp_tool_duration_seconds_total", duration_seconds, tool=tool
        )
        self._registry.inc("scraper_mcp_tool_duration_count", tool=tool)

    def record_session_error(self, code: str) -> None:
        self._registry.inc("scraper_mcp_session_errors_total", code=code)

    # ------------------------------------------------------------- rendering
    def render_prometheus(self) -> str:
        body = self._registry.render()
        uptime = time.time() - _process_started_at
        prefix = "\n".join(
            [
                "# TYPE scraper_mcp_uptime_seconds gauge",
                f"scraper_mcp_uptime_seconds {uptime:.1f}",
                "# TYPE scraper_mcp_tool_calls_total counter",
                f"scraper_mcp_tool_calls_total {self._calls_total}",
            ]
        )
        return prefix + "\n" + body

    @property
    def last_call_at(self) -> float | None:
        return self._last_call_at


_process_started_at = time.time()
