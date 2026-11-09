"""XPath, regex and text-locator-based structured extraction from HTML.

This module extends the original :mod:`.extractor` with support for XPath,
regex, and text locators.  It re-exports the public API from the original
module for compatibility.

The new functionality is provided by this :func:`.extract` which supports
both CSS selectors and new XPath/text-regex locators.
"""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup
from bs4 import Tag as Bs4Tag
from lxml import html as lxml_html
from lxml.etree import _Element as LxmlElement

# Re-export ExtractionError from errors module
from app.scraper.errors import ExtractionError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


MAX_REGEX_LENGTH = 1000
MAX_XPATH_LENGTH = 10000


# ---------------------------------------------------------------------------
# Helpers – element adapter
# ---------------------------------------------------------------------------


class ElementScope:
    """Adapter that exposes text/html/attr extraction for bs4 Tag and lxml Element."""

    def __init__(self, element: Bs4Tag | LxmlElement) -> None:
        self._element = element
        self._is_bs4 = isinstance(element, Bs4Tag)

    def text(self, strip: bool = True) -> str:
        """Extract text content from the element."""
        if self._is_bs4:
            txt = self._element.get_text(strip=strip)
        else:
            txt = "".join(self._element.itertext())
            if strip:
                txt = txt.strip()
        return txt

    def html(self) -> str:
        """Extract inner HTML from the element."""
        if self._is_bs4:
            return "".join(str(c) for c in self._element.children)
        else:
            # lxml tostring of children only (exclude the tag itself)
            from lxml import etree

            parts: list[str] = []
            for child in self._element:
                parts.append(etree.tostring(child, encoding="unicode"))
            return "".join(parts)

    def attr(self, name: str) -> str | None:
        """Extract an attribute value."""
        if self._is_bs4:
            return self._element.get(name)
        else:
            return self._element.get(name)


# ---------------------------------------------------------------------------
# Extended extract function
# ---------------------------------------------------------------------------


def extract(
    html: str,
    fields: dict[str, dict[str, Any]],
    *,
    base_url: str = "",
) -> dict[str, Any]:
    """Extract structured data from *html* using CSS selectors, XPath, or text locators.

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
    # Build lxml tree lazily once per top-level call for xpath/text-regex locators
    lxml_tree = lxml_html.fromstring(html) if _has_xpath_or_text_locator(fields) else None
    result: dict[str, Any] = {}

    for field_name, config in fields.items():
        try:
            result[field_name] = _extract_field(
                soup, lxml_tree, config, base_url, field_name=field_name
            )
        except ExtractionError:
            raise
        except Exception as exc:
            if config.get("required", False):
                raise ExtractionError(field_name, str(exc)) from exc
            result[field_name] = config.get("default")

    return result


def _has_xpath_or_text_locator(fields: dict[str, dict[str, Any]]) -> bool:
    """Return True if any field uses xpath, text_contains, or text_regex."""
    for config in fields.values():
        if isinstance(config, dict):
            if config.get("xpath") or config.get("text_contains") or config.get("text_regex"):
                return True
        # Nested object fields
        nested = config.get("fields")
        if nested and _has_xpath_or_text_locator(nested):
            return True
    return False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_field(
    soup: BeautifulSoup,
    lxml_tree: Any,
    config: dict[str, Any],
    base_url: str,
    field_name: str = "unknown",
) -> Any:
    """Dispatch extraction for a single field configuration."""
    selector: str | None = config.get("selector")
    xpath: str | None = config.get("xpath")
    text_contains: str | None = config.get("text_contains")
    text_regex: str | None = config.get("text_regex")
    regex: str | None = config.get("regex")
    regex_group: int | None = config.get("regex_group")
    field_type: str = config.get("type", "text")
    multiple: bool = config.get("multiple", False)
    required: bool = config.get("required", False)
    default: Any = config.get("default")
    absolute_url: bool = config.get("absolute_url", False)
    attribute: str | None = config.get("attribute")

    # Locator selection
    elements: list[Bs4Tag] = []
    locator_used: str = "selector"

    if selector is not None:
        locator_used = "selector"
        elements = list(soup.select(selector))
    elif xpath is not None:
        locator_used = "xpath"
        if lxml_tree is None:
            # Rebuild tree for this field only if needed (nested object case)
            lxml_tree = lxml_html.fromstring(str(soup))
        locator_elements = _locate_by_xpath(lxml_tree, xpath, field_name)
        # Convert lxml elements to bs4 elements
        from lxml import etree

        for el in locator_elements:
            html_str = etree.tostring(el, encoding="unicode")
            # Parse the HTML string back into bs4 to get a proper Tag
            tmp_soup = BeautifulSoup(html_str, "html.parser")
            if tmp_soup.contents:
                elements.append(tmp_soup.contents[0])
    elif text_contains is not None:
        locator_used = "text_contains"
        elements = list(_locate_by_text_contains(soup, text_contains))
    elif text_regex is not None:
        locator_used = "text_regex"
        elements = list(_locate_by_text_regex(soup, text_regex))
    else:
        # Should not happen (validator enforces exactly one)
        raise ExtractionError(field_name, "No locator provided")

    if not elements:
        if required:
            raise ExtractionError(
                field_name,
                f"No element matched the {locator_used} locator",
            )
        return default

    # Apply regex transform if configured
    if regex is not None:
        if regex_group is None:
            regex_group = 0
        elements = [
            _apply_regex_to_element(
                el, regex, regex_group, field_type, attribute, base_url, field_name
            )
            for el in elements
        ]
        # After regex transform, elements may be None (no match); filter those out
        elements = [e for e in elements if e is not None]
        if not elements:
            if required:
                raise ExtractionError(field_name, "regex did not match any element")
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


def _locate_by_xpath(tree: Any, xpath: str, field_name: str) -> list[LxmlElement]:
    """Locate elements by XPath, raising ExtractionError on bad expression or overflow."""
    # Length guard
    if len(xpath) > MAX_XPATH_LENGTH:
        raise ExtractionError(
            field_name,
            f"XPath expression length {len(xpath)} exceeds maximum {MAX_XPATH_LENGTH}",
        )
    try:
        result = tree.xpath(xpath)
        # lxml may return scalars; convert to list if needed
        if not isinstance(result, list):
            result = [result]
        return [el for el in result if isinstance(el, LxmlElement)]
    except Exception as exc:
        raise ExtractionError(field_name, f"Invalid XPath expression: {exc}") from exc


def _locate_by_text_contains(soup: BeautifulSoup, text: str) -> list[Bs4Tag]:
    """Find elements whose text contains the given string."""
    if not text:
        return []
    found: list[Bs4Tag] = []
    for el in soup.find_all(True):
        el_text = el.get_text(strip=True)
        if text in el_text:
            found.append(el)
    return found


def _locate_by_text_regex(soup: BeautifulSoup, pattern: str) -> list[Bs4Tag]:
    """Find elements whose text matches the regex pattern."""
    # Length guard
    if len(pattern) > MAX_REGEX_LENGTH:
        raise ExtractionError(
            "field",
            f"Regex pattern length {len(pattern)} exceeds maximum {MAX_REGEX_LENGTH}",
        )
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ExtractionError("field", f"Invalid regex pattern: {exc}") from exc

    found: list[Bs4Tag] = []
    for el in soup.find_all(True):
        el_text = el.get_text(strip=True)
        if regex.search(el_text):
            found.append(el)
    return found


def _element_to_html(el: LxmlElement) -> str:
    """Convert lxml element to HTML string suitable for bs4 wrapping."""
    from lxml import etree

    return etree.tostring(el, encoding="unicode")


def _apply_regex_to_element(
    element: Bs4Tag,
    pattern: str,
    group: int,
    field_type: str,
    attribute: str | None,
    base_url: str,
    field_name: str = "unknown",
) -> Bs4Tag | None:
    """Apply regex to the value that would be extracted, return element or None."""
    # Length guard
    if len(pattern) > MAX_REGEX_LENGTH:
        raise ExtractionError(
            "field", f"Regex pattern length {len(pattern)} exceeds maximum {MAX_REGEX_LENGTH}"
        )
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ExtractionError("field", f"Invalid regex pattern: {exc}") from exc

    # Get the primitive value that would be extracted
    el_scope = ElementScope(element)
    if field_type == "attr":
        if attribute:
            value = el_scope.attr(attribute)
        else:
            value = el_scope.attr("href") or el_scope.attr("src")
    elif field_type == "html":
        value = el_scope.html()
    else:  # text (default) or object
        value = el_scope.text()

    if not value:
        return None

    match = regex.search(value)
    if not match:
        return None

    # Replace element's text/attr with the captured group
    # Validate group index against actual matches
    num_groups = len(match.groups())
    if group == 0:
        # Full match (group 0) is always valid
        captured = match.group(0)
    elif group > num_groups:
        raise ExtractionError(
            field_name,
            f"Regex capture group {group} is out of bounds (pattern has {num_groups} groups)",
        )
    else:
        captured = match.group(group)
    if isinstance(captured, str):
        if field_type == "attr":
            if attribute:
                element[attribute] = captured
            else:
                if "href" in element.attrs:
                    element["href"] = captured
                elif "src" in element.attrs:
                    element["src"] = captured
        elif field_type == "html":
            # For HTML, replace inner HTML with the match
            element.clear()
            element.append(captured)
        else:
            # For text, replace entire text
            element.clear()
            element.append(captured)
    return element


def _extract_nested(
    element: Bs4Tag,
    fields: dict[str, Any],
    base_url: str,
) -> dict[str, Any]:
    """Recursively extract nested ``object`` fields from *element*."""
    soup = BeautifulSoup(str(element), "html.parser")
    result: dict[str, Any] = {}
    for field_name, config in fields.items():
        try:
            result[field_name] = _extract_field(soup, None, config, base_url, field_name=field_name)
        except ExtractionError:
            raise
        except Exception as exc:
            if config.get("required", False):
                raise ExtractionError(field_name, str(exc)) from exc
            result[field_name] = config.get("default")
    return result


def _extract_primitive(
    element: Bs4Tag,
    field_type: str,
    attribute: str | None,
    absolute_url: bool,
    base_url: str,
) -> Any:
    """Extract a ``text``, ``html`` or ``attr`` value from *element*."""
    el_scope = ElementScope(element)
    if field_type == "text":
        value = el_scope.text(strip=True)
    elif field_type == "html":
        value = el_scope.html()
    elif field_type == "attr":
        if attribute:
            value = el_scope.attr(attribute)
        else:
            # Fallback to the most common URL-carrying attributes.
            value = el_scope.attr("href") or el_scope.attr("src")

        if absolute_url and value and isinstance(value, str):
            from urllib.parse import urljoin

            value = urljoin(base_url, value)
    else:
        value = str(element)

    return value
