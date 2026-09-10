"""Header allowlist and validation for per-request custom headers."""

from __future__ import annotations

import re

# Standard header names allowed in addition to any ``X-`` prefixed header.
# Compared case-insensitively.
ALLOWED_STANDARD_HEADERS = {"authorization", "referer", "accept-language", "origin"}

# RFC 7230 token grammar for header field names.
_TOKEN_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")

# Characters that must never appear in a header value (CR/LF/NUL enable
# header/request splitting injection).
_FORBIDDEN_VALUE_CHARS = ("\r", "\n", "\0")

MAX_HEADERS = 20
MAX_HEADER_VALUE_LENGTH = 4000


def is_allowed_header_name(name: str) -> bool:
    """Return ``True`` if *name* is permitted by the header allowlist."""
    lowered = name.lower()
    return lowered in ALLOWED_STANDARD_HEADERS or lowered.startswith("x-")


def validate_header_name(name: str) -> str | None:
    """Return an error message if *name* is invalid, else ``None``."""
    if not name or not _TOKEN_RE.match(name):
        return f"Invalid header name: {name!r}"
    if not is_allowed_header_name(name):
        return (
            f"Header {name!r} is not allowed. Allowed: "
            f"{sorted(ALLOWED_STANDARD_HEADERS)} or any header prefixed with 'X-'"
        )
    return None


def validate_header_value(name: str, value: str) -> str | None:
    """Return an error message if *value* is invalid, else ``None``."""
    if any(ch in value for ch in _FORBIDDEN_VALUE_CHARS):
        return f"Header {name!r} value contains forbidden control characters"
    if len(value) > MAX_HEADER_VALUE_LENGTH:
        return f"Header {name!r} value exceeds {MAX_HEADER_VALUE_LENGTH} characters"
    return None


def parse_header_config(header_config: dict | None) -> dict[str, str]:
    """Parse and defense-in-depth validate request header configuration.

    Returns only headers that pass the allowlist and value checks. This is a
    second line of defense — the primary validation happens in
    ``HeaderConfig`` at the schema layer, which rejects invalid requests
    with a 422 before they reach the service.
    """
    if not header_config or not header_config.get("enabled", False):
        return {}

    values = header_config.get("values") or {}
    result: dict[str, str] = {}
    for name, value in values.items():
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        if validate_header_name(name) is not None:
            continue
        if validate_header_value(name, value) is not None:
            continue
        result[name] = value
    return result
