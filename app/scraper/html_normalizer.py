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

_MEDIA_TAGS = {
    "audio",
    "canvas",
    "embed",
    "iframe",
    "img",
    "object",
    "picture",
    "source",
    "svg",
    "video",
}

_PRESETS: dict[str, set[str]] = {
    "light": {
        "remove_comments",
        "remove_noscript",
        "collapse_whitespace",
        "minify",
    },
    "content": {
        "remove_scripts",
        "remove_styles",
        "remove_comments",
        "remove_meta",
        "remove_noscript",
        "remove_hidden",
        "remove_data_attrs",
        "collapse_whitespace",
        "minify",
    },
    "aggressive": {
        "remove_scripts",
        "remove_styles",
        "remove_comments",
        "remove_meta",
        "remove_noscript",
        "remove_hidden",
        "remove_media",
        "remove_data_attrs",
        "collapse_whitespace",
        "minify",
    },
}

_re_whitespace = re.compile(r"\s+")
_re_hidden_style = re.compile(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", re.I)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize_html(
    html: str,
    base_url: str,
    *,
    enabled: bool = False,
    preset: str | None = None,
    absolute_urls: bool = False,
    remove_scripts: bool = False,
    remove_styles: bool = False,
    remove_comments: bool = False,
    remove_meta: bool = False,
    remove_noscript: bool = False,
    remove_data_attrs: bool = False,
    remove_hidden: bool = False,
    remove_media: bool = False,
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
        Master switch. A non-empty preset also enables normalization.
    preset:
        Optional preset: ``light``, ``content`` or ``aggressive``.

    All other boolean parameters are individual normalisation features.

    Returns
    -------
    tuple[str, dict[str, bool]]
        ``(normalized_html, applied_features)`` where *applied_features*
        contains only the features that were actually active.
    """
    preset_features = _PRESETS.get(preset or "", set())
    if not enabled and not preset_features:
        return html, {}

    absolute_urls = absolute_urls or "absolute_urls" in preset_features
    remove_scripts = remove_scripts or "remove_scripts" in preset_features
    remove_styles = remove_styles or "remove_styles" in preset_features
    remove_comments = remove_comments or "remove_comments" in preset_features
    remove_meta = remove_meta or "remove_meta" in preset_features
    remove_noscript = remove_noscript or "remove_noscript" in preset_features
    remove_data_attrs = remove_data_attrs or "remove_data_attrs" in preset_features
    remove_hidden = remove_hidden or "remove_hidden" in preset_features
    remove_media = remove_media or "remove_media" in preset_features
    collapse_whitespace = collapse_whitespace or "collapse_whitespace" in preset_features
    minify = minify or "minify" in preset_features

    soup = BeautifulSoup(html, "html.parser")
    applied: dict[str, bool] = {}

    if remove_hidden:
        _remove_hidden(soup)
        applied["remove_hidden"] = True

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

    if remove_media:
        _remove_media(soup)
        applied["remove_media"] = True

    if remove_data_attrs:
        _remove_data_attrs(soup)
        applied["remove_data_attrs"] = True

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


def html_to_text(html: str) -> str:
    """Convert HTML to compact visible text."""
    soup = BeautifulSoup(html, "html.parser")
    for tag_name in ("script", "style", "noscript", "template"):
        _remove_tags(soup, tag_name)
    text = soup.get_text("\n", strip=True)
    lines = [_re_whitespace.sub(" ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


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


def _remove_data_attrs(soup: BeautifulSoup) -> None:
    """Strip all ``data-*`` attributes."""
    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue
        for attr in list(tag.attrs):
            if attr.startswith("data-"):
                tag.attrs.pop(attr, None)


def _remove_hidden(soup: BeautifulSoup) -> None:
    """Remove nodes that are explicitly hidden from the rendered page."""
    _remove_tags(soup, "template")
    for tag in list(soup.find_all(True)):
        if not isinstance(tag, Tag) or tag.parent is None:
            continue

        style = tag.attrs.get("style", "")
        tag_type = tag.attrs.get("type", "")
        aria_hidden = tag.attrs.get("aria-hidden")
        is_hidden = (
            tag.has_attr("hidden")
            or (isinstance(aria_hidden, str) and aria_hidden.lower() == "true")
            or (isinstance(style, str) and _re_hidden_style.search(style) is not None)
            or (tag.name == "input" and isinstance(tag_type, str) and tag_type.lower() == "hidden")
        )
        if is_hidden:
            tag.decompose()


def _remove_media(soup: BeautifulSoup) -> None:
    """Remove media and embedded-content tags."""
    for tag_name in _MEDIA_TAGS:
        _remove_tags(soup, tag_name)


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
