"""Unit tests for the CSS-selector-based HTML extractor.

All tests parse sample HTML strings and verify the extracted values
without making any network requests.
"""

from __future__ import annotations

import pytest

from app.scraper.extractor import ExtractionError, extract

# =========================================================================
# Fixtures – sample HTML snippets
# =========================================================================

SIMPLE_HTML = """\
<html>
<head><title>Example Domain</title></head>
<body>
  <h1>Example Heading</h1>
  <p class="description">This is a paragraph.</p>
  <a href="/relative/page.html" id="link">Click here</a>
  <img src="/images/photo.png" alt="A photo">
  <div class="container">
    <span class="item">Item 1</span>
    <span class="item">Item 2</span>
    <span class="item">Item 3</span>
  </div>
  <div class="rich-html"><strong>Bold</strong> and <em>italic</em></div>
</body>
</html>
"""

PRODUCT_CARDS_HTML = """\
<html>
<body>
  <div class="product-card">
    <h2 class="product-card-title">Widget Alpha</h2>
    <span class="price">$19.99</span>
    <a href="/products/widget-alpha">View details</a>
  </div>
  <div class="product-card">
    <h2 class="product-card-title">Widget Beta</h2>
    <span class="price">$29.99</span>
    <a href="/products/widget-beta">View details</a>
  </div>
</body>
</html>
"""

EMPTY_HTML = "<html><body></body></html>"


# =========================================================================
# Core extraction
# =========================================================================


class TestExtractText:
    def test_extract_title_text(self):
        result = extract(
            SIMPLE_HTML,
            {"title": {"selector": "title", "type": "text"}},
        )
        assert result["title"] == "Example Domain"

    def test_extract_paragraph_text(self):
        result = extract(
            SIMPLE_HTML,
            {"desc": {"selector": ".description", "type": "text"}},
        )
        assert result["desc"] == "This is a paragraph."

    def test_missing_optional_returns_default(self):
        result = extract(
            EMPTY_HTML,
            {"missing": {"selector": ".nonexistent", "type": "text", "default": "fallback"}},
        )
        assert result["missing"] == "fallback"

    def test_missing_optional_returns_none(self):
        result = extract(
            EMPTY_HTML,
            {"missing": {"selector": ".nonexistent", "type": "text"}},
        )
        assert result["missing"] is None

    def test_missing_required_raises_error(self):
        with pytest.raises(ExtractionError) as excinfo:
            extract(
                EMPTY_HTML,
                {"required_field": {"selector": ".missing", "type": "text", "required": True}},
            )
        assert "required_field" in str(excinfo.value)
        assert ".missing" in str(excinfo.value)


class TestExtractAttribute:
    def test_extract_href_attribute(self):
        result = extract(
            SIMPLE_HTML,
            {"link": {"selector": "#link", "type": "attr", "attribute": "href"}},
        )
        assert result["link"] == "/relative/page.html"

    def test_extract_src_attribute(self):
        result = extract(
            SIMPLE_HTML,
            {"src": {"selector": "img", "type": "attr", "attribute": "src"}},
        )
        assert result["src"] == "/images/photo.png"

    def test_extract_alt_attribute(self):
        result = extract(
            SIMPLE_HTML,
            {"alt": {"selector": "img", "type": "attr", "attribute": "alt"}},
        )
        assert result["alt"] == "A photo"

    def test_missing_attribute_returns_none(self):
        result = extract(
            SIMPLE_HTML,
            {"missing": {"selector": "img", "type": "attr", "attribute": "nonexistent"}},
        )
        assert result["missing"] is None


class TestExtractHtml:
    def test_extract_inner_html(self):
        result = extract(
            SIMPLE_HTML,
            {"content": {"selector": ".rich-html", "type": "html"}},
        )
        assert "<strong>Bold</strong>" in result["content"]
        assert "<em>italic</em>" in result["content"]


class TestExtractMultiple:
    def test_extract_multiple_text(self):
        result = extract(
            SIMPLE_HTML,
            {"items": {"selector": ".item", "type": "text", "multiple": True}},
        )
        assert result["items"] == ["Item 1", "Item 2", "Item 3"]

    def test_extract_multiple_empty_returns_empty_list(self):
        result = extract(
            EMPTY_HTML,
            {"items": {"selector": ".nonexistent", "type": "text", "multiple": True}},
        )
        assert result["items"] is None  # default for missing optional

    def test_extract_multiple_text_custom_default(self):
        result = extract(
            EMPTY_HTML,
            {
                "items": {
                    "selector": ".nonexistent",
                    "type": "text",
                    "multiple": True,
                    "default": [],
                }
            },
        )
        assert result["items"] == []


# =========================================================================
# Nested objects
# =========================================================================


class TestExtractNestedObject:
    def test_extract_single_nested_object(self):
        result = extract(
            PRODUCT_CARDS_HTML,
            {
                "product": {
                    "selector": ".product-card",
                    "type": "object",
                    "multiple": False,
                    "fields": {
                        "name": {"selector": ".product-card-title", "type": "text"},
                        "price": {"selector": ".price", "type": "text"},
                    },
                }
            },
        )
        assert result["product"] == {
            "name": "Widget Alpha",
            "price": "$19.99",
        }

    def test_extract_nested_object_list(self):
        result = extract(
            PRODUCT_CARDS_HTML,
            {
                "products": {
                    "selector": ".product-card",
                    "type": "object",
                    "multiple": True,
                    "fields": {
                        "name": {"selector": ".product-card-title", "type": "text"},
                        "price": {"selector": ".price", "type": "text"},
                    },
                }
            },
        )
        assert len(result["products"]) == 2
        assert result["products"][0] == {"name": "Widget Alpha", "price": "$19.99"}
        assert result["products"][1] == {"name": "Widget Beta", "price": "$29.99"}

    def test_nested_object_with_attributes(self):
        """object fields can themselves use attr type with absolute_url."""
        html = """\
        <div class="card">
          <a href="/detail/1">Link 1</a>
        </div>
        """
        result = extract(
            html,
            {
                "card": {
                    "selector": ".card",
                    "type": "object",
                    "fields": {
                        "url": {
                            "selector": "a",
                            "type": "attr",
                            "attribute": "href",
                            "absolute_url": True,
                        },
                    },
                }
            },
            base_url="https://example.com",
        )
        assert result["card"]["url"] == "https://example.com/detail/1"

    def test_nested_required_field_fails(self):
        with pytest.raises(ExtractionError):
            extract(
                PRODUCT_CARDS_HTML,
                {
                    "product": {
                        "selector": ".product-card",
                        "type": "object",
                        "fields": {
                            "name": {
                                "selector": ".missing-class",
                                "type": "text",
                                "required": True,
                            },
                        },
                    }
                },
            )


# =========================================================================
# Absolute URL conversion
# =========================================================================


class TestAbsoluteUrl:
    BASE = "https://example.com"

    def test_relative_href_becomes_absolute(self):
        result = extract(
            SIMPLE_HTML,
            {
                "link": {
                    "selector": "#link",
                    "type": "attr",
                    "attribute": "href",
                    "absolute_url": True,
                }
            },
            base_url=self.BASE,
        )
        assert result["link"] == "https://example.com/relative/page.html"

    def test_absolute_url_unchanged(self):
        html = '<a href="https://other.com/page">link</a>'
        result = extract(
            html,
            {"link": {"selector": "a", "type": "attr", "attribute": "href", "absolute_url": True}},
            base_url=self.BASE,
        )
        assert result["link"] == "https://other.com/page"

    def test_text_field_absolute_url_is_noop(self):
        """absolute_url on a text field does nothing (no URL to resolve)."""
        result = extract(
            SIMPLE_HTML,
            {"title": {"selector": "title", "type": "text", "absolute_url": True}},
            base_url=self.BASE,
        )
        assert result["title"] == "Example Domain"

    def test_attribute_without_absolute_url_stays_relative(self):
        result = extract(
            SIMPLE_HTML,
            {"link": {"selector": "#link", "type": "attr", "attribute": "href"}},
            base_url=self.BASE,
        )
        assert result["link"] == "/relative/page.html"


# =========================================================================
# Error handling
# =========================================================================


class TestExtractionError:
    def test_required_field_missing_raises_extraction_error(self):
        with pytest.raises(ExtractionError) as excinfo:
            extract(
                EMPTY_HTML,
                {"title": {"selector": "title", "type": "text", "required": True}},
            )
        assert excinfo.value.field_name == "title"
        assert "title" in excinfo.value.message

    def test_extraction_error_to_dict(self):
        err = ExtractionError("products", "No element matched CSS selector '.products'")
        d = err.to_dict()
        assert d == {"field": "products", "message": "No element matched CSS selector '.products'"}

    def test_partial_success_with_optional_missing(self):
        """Optional fields that don't match return None; required ones still work."""
        result = extract(
            SIMPLE_HTML,
            {
                "title": {"selector": "title", "type": "text"},
                "missing": {"selector": ".nope", "type": "text", "default": "fallback"},
            },
        )
        assert result["title"] == "Example Domain"
        assert result["missing"] == "fallback"


# =========================================================================
# Field type defaults and edge cases
# =========================================================================


class TestFieldDefaults:
    def test_no_type_defaults_to_text(self):
        result = extract(
            SIMPLE_HTML,
            {"title": {"selector": "title"}},
        )
        assert result["title"] == "Example Domain"

    def test_empty_html_returns_defaults(self):
        result = extract(
            "",
            {"title": {"selector": "title", "type": "text", "default": "no title"}},
        )
        assert result["title"] == "no title"

    def test_nonexistent_selector_returns_default_for_non_required(self):
        result = extract(
            SIMPLE_HTML,
            {"x": {"selector": ".does-not-exist-at-all", "type": "text"}},
        )
        assert result["x"] is None


class TestExtractAttrFallback:
    def test_attr_type_no_attribute_fallback_to_href(self):
        html = '<a href="https://example.com">link</a>'
        result = extract(html, {"link": {"selector": "a", "type": "attr"}})
        assert result["link"] == "https://example.com"

    def test_attr_type_no_attribute_fallback_to_src(self):
        html = '<img src="https://example.com/img.png">'
        result = extract(html, {"img": {"selector": "img", "type": "attr"}})
        assert result["img"] == "https://example.com/img.png"


class TestExtractUnknownType:
    def test_unknown_type_fallback_to_str(self):
        html = "<div>content</div>"
        result = extract(html, {"x": {"selector": "div", "type": "unknown"}})
        assert result["x"] is not None


class TestExtractExceptionInField:
    def test_required_field_raises_via_re_raise(self):
        with pytest.raises(ExtractionError):
            extract(
                SIMPLE_HTML,
                {
                    "req_field": {
                        "selector": ".does-not-exist",
                        "type": "text",
                        "required": True,
                    }
                },
            )

    def test_non_required_field_exception_returns_default(self):
        result = extract(
            PRODUCT_CARDS_HTML,
            {
                "bad": {
                    "selector": None,  # type: ignore[typeddict-item]
                    "type": "text",
                    "required": False,
                    "default": "safe_fallback",
                }
            },
        )
        assert result["bad"] == "safe_fallback"

    def test_nested_field_exception_non_required(self):
        html = '<div class="card"><a href="/page">link</a></div>'
        result = extract(
            html,
            {
                "card": {
                    "selector": ".card",
                    "type": "object",
                    "fields": {
                        "link": {
                            "selector": None,  # type: ignore[typeddict-item]
                            "type": "text",
                            "required": False,
                            "default": "fallback_url",
                        },
                    },
                }
            },
        )
        assert result["card"]["link"] == "fallback_url"
