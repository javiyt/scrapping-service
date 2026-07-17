"""Response-time transformations for scrape results."""

from typing import Any

from app.schemas.scrape import ExtractConfig
from app.scraper.extractor import ExtractionError
from app.scraper.extractor import extract as run_extraction
from app.scraper.html_normalizer import html_to_text, normalize_html


def apply_normalization(
    result: dict[str, Any],
    normalize_config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Apply HTML normalisation to a copy of *result* if requested."""
    if not normalize_config:
        return result

    if not normalize_config.get("enabled") and not normalize_config.get("preset"):
        return result

    raw_html = result.get("html", "")
    base_url = result.get("final_url", "")
    if not raw_html:
        return result

    normalized_html, applied = normalize_html(raw_html, base_url, **normalize_config)
    if not applied:
        return result

    metadata = dict(result.get("metadata", {}))
    metadata["normalized"] = True
    metadata["normalization"] = applied
    metadata["content_length"] = len(normalized_html)

    return {
        **result,
        "html": normalized_html,
        "metadata": metadata,
    }


def apply_extraction(
    result: dict[str, Any],
    extract_config: ExtractConfig | dict[str, Any] | None,
) -> dict[str, Any]:
    """Apply CSS-selector-based extraction to a response dict if requested."""
    if extract_config is None:
        return result
    if isinstance(extract_config, dict):
        extract_config = ExtractConfig.model_validate(extract_config)
    if not extract_config.enabled or not extract_config.fields:
        return result

    html_to_extract = result.get("html", "")
    if not html_to_extract:
        return result

    fields = {name: field.model_dump() for name, field in extract_config.fields.items()}
    base_url = extract_config.base_url or result.get("final_url") or result.get("url", "")

    try:
        extracted_data = run_extraction(html_to_extract, fields, base_url=base_url)
        return {**result, "extracted": extracted_data}
    except ExtractionError as exc:
        return {
            **result,
            "extracted": None,
            "extraction_error": exc.to_dict(),
        }


def format_scrape_content(
    result: dict[str, Any],
    response_format: str = "html",
) -> dict[str, Any]:
    """Return a v2 response dict with ``content`` instead of ``html``."""
    metadata = dict(result.get("metadata", {}))
    if response_format == "html":
        content = result.get("html", "")
    else:
        content = html_to_text(result.get("html", ""))

    metadata["response_format"] = response_format
    metadata["content_length"] = len(content)

    formatted = {key: value for key, value in result.items() if key != "html"}
    formatted["content"] = content
    formatted["metadata"] = metadata
    return formatted


def process_scrape_response(
    result: dict[str, Any],
    *,
    normalize_config: dict[str, Any] | None = None,
    extract_config: ExtractConfig | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run all response-time transformations in API order."""
    result = apply_normalization(result, normalize_config)
    return apply_extraction(result, extract_config)
