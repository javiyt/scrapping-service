"""HTML normalization using BeautifulSoup.

Transforms raw HTML according to per-request configuration without modifying
the cached content.  Normalization is applied at response time only.
"""

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# HTML attributes that can carry URLs.
_URL_ATTRS = {
    "href",  # a, area, link, base
    "src",  # img, script, video, audio, source, iframe, embed
    "action",  # form
    "poster",  # video
}

# Tags whose content should be left alone during whitespace collapsing.
_PRESERVE_WHITESPACE_TAGS = {"pre", "code", "textarea", "script", "style"}

_re_whitespace = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize_html(
    html: str,
    base_url: str,
    *,
    enabled: bool = False,
    absolute_urls: bool = False,
    remove_scripts: bool = False,
    remove_styles: bool = False,
    remove_comments: bool = False,
    remove_meta: bool = False,
    remove_noscript: bool = False,
    collapse_whitespace: bool = False,
    minify: bool = False,
) -> tuple[str, dict[str, bool]]:
    """Apply optional normalizations to *html* and return the result.

    Parameters
    ----------
    html:
        Raw HTML string.
    base_url:
        The final (fully-resolved) URL of the page, used as the base when
        converting relative URLs to absolute.
    enabled:
        Master switch — when ``False`` the original HTML is returned as-is
        and the applied-features dict is empty.

    All other boolean parameters are individual normalisation features.

    Returns
    -------
    tuple[str, dict[str, bool]]
        ``(normalized_html, applied_features)`` where *applied_features*
        contains only the features that were actually active.
    """
    if not enabled:
        return html, {}

    soup = BeautifulSoup(html, "html.parser")
    applied: dict[str, bool] = {}

    if remove_scripts:
        _remove_tags(soup, "script")
        applied["remove_scripts"] = True

    if remove_styles:
        _remove_tags(soup, "style")
        _remove_inline_style_attrs(soup)
        applied["remove_styles"] = True

    if remove_comments:
        _remove_comments(soup)
        applied["remove_comments"] = True

    if remove_meta:
        _remove_tags(soup, "meta")
        applied["remove_meta"] = True

    if remove_noscript:
        _remove_tags(soup, "noscript")
        applied["remove_noscript"] = True

    if absolute_urls:
        _make_urls_absolute(soup, base_url)
        applied["absolute_urls"] = True

    if collapse_whitespace:
        _collapse_whitespace(soup)
        applied["collapse_whitespace"] = True

    if minify:
        _minify(soup)
        applied["minify"] = True

    normalized = str(soup)
    return normalized, applied


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _remove_tags(soup: BeautifulSoup, tag_name: str) -> None:
    """Remove every occurrence of *tag_name* from the tree."""
    for tag in soup.find_all(tag_name):
        tag.decompose()


def _remove_inline_style_attrs(soup: BeautifulSoup) -> None:
    """Strip ``style`` attributes from all elements."""
    for tag in soup.find_all(True):  # every tag
        if isinstance(tag, Tag):
            tag.attrs.pop("style", None)


def _remove_comments(soup: BeautifulSoup) -> None:
    """Remove HTML comments."""
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()


def _make_urls_absolute(soup: BeautifulSoup, base_url: str) -> None:
    """Convert relative URLs in common attributes to absolute.

    Handles ``href``, ``src``, ``action``, ``poster``, and the first level
    of ``srcset``.
    """
    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue

        for attr in _URL_ATTRS:
            val = tag.attrs.get(attr)
            if val and isinstance(val, str):
                tag.attrs[attr] = urljoin(base_url, val)

        # ---------- srcset ----------
        srcset = tag.attrs.get("srcset")
        if srcset and isinstance(srcset, str):
            tag.attrs["srcset"] = _resolve_srcset(srcset, base_url)


def _resolve_srcset(srcset: str, base_url: str) -> str:
    """Resolve each URL in a ``srcset`` attribute against *base_url*.

    A ``srcset`` is a comma-separated list where each entry is
    ``<url> [descriptor]`` (e.g. ``image-320w.jpg 320w,``).
    We only modify the URL portion of each entry.
    """
    entries = []
    for entry in srcset.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(None, 1)  # url [optional descriptor]
        url_part = parts[0]
        resolved = urljoin(base_url, url_part)
        if len(parts) > 1:
            entries.append(f"{resolved} {parts[1]}")
        else:
            entries.append(resolved)
    return ", ".join(entries)


def _collapse_whitespace(soup: BeautifulSoup) -> None:
    """Replace runs of whitespace in text nodes with a single space.

    Content inside ``<pre>``, ``<code>``, ``<textarea>``, ``<script>`` and
    ``<style>`` is left untouched.
    """
    _collapse_node(soup)


def _collapse_node(node: Tag | NavigableString) -> None:
    """Recursively collapse whitespace in *node* and its children."""
    # ---- Leaf text node ----
    if isinstance(node, NavigableString) and not isinstance(node, Comment):
        if node.parent and getattr(node.parent, "name", None) in _PRESERVE_WHITESPACE_TAGS:
            return
        new_text = _re_whitespace.sub(" ", str(node))
        if new_text != str(node):
            node.replace_with(NavigableString(new_text))
        return

    # ---- Tag / container ----
    if isinstance(node, Tag):
        for child in list(node.children):
            _collapse_node(child)


def _minify(soup: BeautifulSoup) -> None:
    """Compact HTML output without breaking semantics.

    - Removes whitespace-only text nodes between block-level elements.
    - Strips leading/trailing whitespace from visible text nodes.
    - Preserves ``<pre>``, ``<code>``, ``<textarea>`` content.
    """
    for child in list(soup.children):
        _minify_node(child)


def _minify_node(node: Tag | NavigableString) -> None:
    """Recursively minify a single node."""
    if isinstance(node, NavigableString) and not isinstance(node, Comment):
        parent_name = node.parent.name if node.parent else None
        if parent_name in _PRESERVE_WHITESPACE_TAGS:
            return

        text = str(node)
        stripped = text.strip()
        if not stripped:
            # Whitespace-only text node — remove entirely.
            node.extract()
        elif stripped != text:
            node.replace_with(NavigableString(stripped))
        return

    if isinstance(node, Tag):
        for child in list(node.children):
            _minify_node(child)
