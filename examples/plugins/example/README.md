"""Example plugin for scrapping-service.

This plugin demonstrates the scrapping-service plugin API v1:

- A SearchProvider with async capabilities() and search()
- Per-domain extractors (CSS and custom)
- Configuration via SCRAPER_PLUGIN_EXAMPLE_* env vars

## Installation in a derived Docker image

```dockerfile
FROM ghcr.io/javiyt/scrapping-service:latest
USER root
COPY example /tmp/example
RUN pip install --no-cache-dir /tmp/example
USER scraper
CMD ["python", "app/run.py"]
```

## Install in a development venv

```bash
# From the scrapping-service repo root
pip install -e examples/plugins/example
python -mscrapping-service
```

## Configuration

The plugin respects the `SCRAPER_PLUGIN_EXAMPLE_*` namespace:

- `SCRAPER_PLUGIN_EXAMPLE_TIMEOUT_SECONDS` — provider timeout (default: 30)
- `SCRAPER_PLUGIN_EXAMPLE_DEBUG_ENABLED` — enable debug logging (default: false)

## Example contract tests

The service's `app.plugin_testing` module provides reusable contract suites.
Run them against this plugin in CI or locally:

```bash
# In the service repo (after pip install -e examples/plugins/example)
pytest tests/test_plugin_testing.py -v
```