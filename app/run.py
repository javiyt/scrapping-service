#!/usr/bin/env python
"""Wrapper script to start uvicorn with normalized environment variables.

Reads environment variables and command-line arguments, normalizing them
before passing to uvicorn. Allows case-insensitive values like LOG_LEVEL=INFO
to work correctly. Command-line arguments override environment variables.
"""

import argparse
import os
import sys


def normalize_log_level(log_level: str) -> str:
    """Normalize log level to lowercase and validate."""
    log_level = log_level.lower()
    valid_levels = {"critical", "error", "warning", "info", "debug", "trace"}
    if log_level not in valid_levels:
        raise ValueError(
            f"Invalid log level '{log_level}'; must be one of: {', '.join(sorted(valid_levels))}"
        )
    return log_level


def main():
    """Start uvicorn with normalized environment variables and CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Start Scraper API with normalized configuration",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python app/run.py
  python app/run.py --port 9090
  python app/run.py --port 9090 --log-level debug
  python app/run.py --log-level warning --limit-max-requests 1000
        """,
    )
    parser.add_argument(
        "-p",
        "--port",
        type=str,
        help="Port to listen on (default: SCRAPER_SERVER_PORT env or 8080)",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        help="Log level: critical, error, warning, info, debug, trace "
        "(accepts any case; default: LOG_LEVEL env or info)",
    )
    parser.add_argument(
        "--timeout-keep-alive",
        type=str,
        help="Keep-alive timeout in seconds (default: TIMEOUT_KEEP_ALIVE env or 30)",
    )
    parser.add_argument(
        "--limit-max-requests",
        type=str,
        help="Max requests per worker before restart (default: LIMIT_MAX_REQUESTS env or 5000)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload on file changes (development mode)",
    )

    args = parser.parse_args()

    # Resolve configuration: CLI args > environment > defaults
    port = args.port or os.environ.get("SCRAPER_SERVER_PORT", "8080")
    log_level = args.log_level or os.environ.get("LOG_LEVEL", "info")
    timeout_keep_alive = args.timeout_keep_alive or os.environ.get("TIMEOUT_KEEP_ALIVE", "30")
    limit_max_requests = args.limit_max_requests or os.environ.get("LIMIT_MAX_REQUESTS", "5000")
    reload = args.reload or os.environ.get("DEVELOPMENT") == "1"

    # Normalize and validate log level
    try:
        log_level = normalize_log_level(log_level)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Build uvicorn command
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        port,
        "--log-level",
        log_level,
        "--timeout-keep-alive",
        timeout_keep_alive,
        "--limit-max-requests",
        limit_max_requests,
    ]

    if reload:
        cmd.insert(cmd.index("--host"), "--reload")

    os.execvp(sys.executable, cmd)


if __name__ == "__main__":
    main()
