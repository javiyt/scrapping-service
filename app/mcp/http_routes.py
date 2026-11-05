"""Ops routes for the streamable-HTTP adapter mode (ticket #153).

- ``/health``  — adapter process liveness *plus* a proxied liveness of the
  service (the adapter is useless when the service is down; the two
  statuses are reported separately).
- ``/metrics`` — the adapter's own Prometheus metrics (tool calls,
  durations, session errors, uptime).  The service's native metrics stay
  reachable on the service's own ``/metrics``; this endpoint does not
  modify that output.
"""

from __future__ import annotations

import time
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from app.mcp.client import ScrapperClient
from app.mcp.observability import MCPMetrics

_started_at = time.time()


def health_route(client: ScrapperClient, metrics: MCPMetrics) -> Any:
    async def health(request: Request) -> JSONResponse:
        try:
            response = await client.get("/health")
            service_status: int = response.status_code
            service_body: Any = _safe_json(response)
        except Exception as exc:  # noqa: BLE001 — mapped error type only, no message
            service_status = 503
            service_body = {"error": {"type": type(exc).__name__}}
        return JSONResponse(
            {
                "status": "ok",
                "adapter": {
                    "up": True,
                    "uptime_seconds": round(time.time() - _started_at, 1),
                    "last_tool_call_at": metrics.last_call_at,
                },
                "service": {
                    "status_code": service_status,
                    "reachable": service_status == 200,
                    "body": service_body,
                },
            }
        )

    return health


def metrics_route(metrics: MCPMetrics, client: ScrapperClient) -> Any:
    registry_metrics = metrics  # shadow-safe name for the closure

    async def _metrics(request: Request) -> PlainTextResponse:
        return PlainTextResponse(
            registry_metrics.render_prometheus(), media_type="text/plain; version=0.0.4"
        )

    return _metrics


def _safe_json(response: Any) -> Any:
    try:
        return response.json()
    except Exception:  # noqa: BLE001
        return None
