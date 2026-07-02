"""CSS selector-based structured extraction from HTML.

Provides the :func:`extract` function that takes raw (or normalised) HTML
and a set of field configurations to produce a structured ``dict`` of
extracted values.  Intended to be called at response time, *after* any
optional HTML normalisation has been applied.

Supported field types
---------------------
* ``text`` — inner text content (leading/trailing whitespace stripped).
* ``html`` — inner HTML markup (preserves tag structure).
* ``attr`` — value of a named HTML attribute.
* ``object`` — nested extraction via sub-``fields``.

Supported options
-----------------
* ``selector`` — CSS selector to locate the target element(s).
* ``type`` — one of the four field types above.
* ``attribute`` — attribute name to read (``attr`` type only).
* ``multiple`` — when ``True``, return a list of all matching results.
* ``default`` — fallback value when no element matches (optional fields).
* ``required`` — when ``True``, a failed match raises :exc:`ExtractionError`.
* ``absolute_url`` — resolve relative URLs against the page URL.
* ``fields`` — nested field definitions (``object`` type only).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class ExtractionError(Exception):
    """Raised when a required field cannot be extracted.

    The exception is caught by the API route and converted to a structured
    ``extraction_error`` dict in the response body.
    """

    def __init__(self, field_name: str, message: str) -> None:
        self.field_name = field_name
        self.message = message
        super().__init__(f"Extraction failed for '{field_name}': {message}")

    def to_dict(self) -> dict[str, Any]:
        """Return the error as a serialisable dict."""
        return {
            "field": self.field_name,
            "message": self.message,
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract(
    html: str,
    fields: dict[str, dict[str, Any]],
    *,
    base_url: str = "",
) -> dict[str, Any]:
    """Extract structured data from *html* using CSS selectors.

    Parameters
    ----------
    html:
        Raw (or pre-normalised) HTML string.
    fields:
        Mapping of field names to their configuration dicts.  Each config
        supports the keys described in the module docstring.
    base_url:
        The page URL used when resolving relative URLs (``absolute_url``).
        Normally this is the page's ``final_url``, optionally overridden by
        the extract config's ``base_url``.

    Returns
    -------
    dict
        Extracted values keyed by field name.

    Raises
    ------
    ExtractionError
        If a field with ``required=True`` could not be resolved.
    """
    soup = BeautifulSoup(html, "html.parser")
    result: dict[str, Any] = {}

    for field_name, config in fields.items():
        try:
            result[field_name] = _extract_field(soup, config, base_url, field_name=field_name)
        except ExtractionError:
            raise
        except Exception as exc:
            if config.get("required", False):
                raise ExtractionError(field_name, str(exc)) from exc
            result[field_name] = config.get("default")

    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_field(
    soup_or_tag: BeautifulSoup | Tag,
    config: dict[str, Any],
    base_url: str,
    field_name: str = "unknown",
) -> Any:
    """Dispatch extraction for a single field configuration."""
    selector: str = config["selector"]
    field_type: str = config.get("type", "text")
    multiple: bool = config.get("multiple", False)
    required: bool = config.get("required", False)
    default: Any = config.get("default")
    absolute_url: bool = config.get("absolute_url", False)
    attribute: str | None = config.get("attribute")

    elements = soup_or_tag.select(selector)

    if not elements:
        if required:
            raise ExtractionError(
                field_name,
                f"No element matched CSS selector '{selector}'",
            )
        return default

    if field_type == "object":
        nested_fields: dict[str, Any] = config.get("fields", {})
        if multiple:
            return [_extract_nested(el, nested_fields, base_url) for el in elements]
        return _extract_nested(elements[0], nested_fields, base_url)

    if multiple:
        return [
            _extract_primitive(el, field_type, attribute, absolute_url, base_url) for el in elements
        ]

    return _extract_primitive(elements[0], field_type, attribute, absolute_url, base_url)


def _extract_nested(
    element: Tag,
    fields: dict[str, Any],
    base_url: str,
) -> dict[str, Any]:
    """Recursively extract nested ``object`` fields from *element*."""
    result: dict[str, Any] = {}
    for field_name, config in fields.items():
        try:
            result[field_name] = _extract_field(element, config, base_url, field_name=field_name)
        except ExtractionError:
            raise
        except Exception as exc:
            if config.get("required", False):
                raise ExtractionError(field_name, str(exc)) from exc
            result[field_name] = config.get("default")
    return result


def _extract_primitive(
    element: Tag,
    field_type: str,
    attribute: str | None,
    absolute_url: bool,
    base_url: str,
) -> Any:
    """Extract a ``text``, ``html`` or ``attr`` value from *element*."""
    if field_type == "text":
        value = element.get_text(strip=True)
    elif field_type == "html":
        value = "".join(str(c) for c in element.children)
    elif field_type == "attr":
        if attribute:
            value = element.get(attribute)
        else:
            # Fallback to the most common URL-carrying attributes.
            value = element.get("href") or element.get("src")

        if absolute_url and value and isinstance(value, str):
            value = urljoin(base_url, value)
    else:
        value = str(element)

    return value
