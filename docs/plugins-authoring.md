# Plugin Authoring Guide

This guide covers how to write a third-party plugin for scrapping-service.

## Overview

A plugin is a Python package that registers one or more providers into the service at startup
via Python [entry points] in the group `scrapping_service.plugins`.

Plugins **cannot** add arbitrary FastAPI routes or MCP tools in v1; those capabilities require
security review and are deferred to post-v1.

[entry points]: https://packaging.python.org/en/latest/specifications/entry-points/

## Plugin model

A plugin must implement the `Plugin` protocol:

```python
from app.plugin_api import Plugin, PluginContext, PluginRegistry


class MyPlugin(Plugin):
    name = "my-plugin"
    version = "1.2.3"
    api_version = "1.0.0"  # Must match PLUGIN_API_VERSION major

    def register(self, registry: PluginRegistry, ctx: PluginContext) -> None:
        # Register providers here
        pass
```

`Plugin` is a `@runtime_checkable` `Protocol`, so your class does not need to subclass it
explicitly: any class with `name`, `version`, `api_version`, and `register()` satisfies it.
Subclassing is still valid and helps editors.

### Plugin lifecycle

1. **Discovery**: The loader scans Python environments for entry points in the group
   `scrapping_service.plugins`.
2. **Validation**: Each discovered entry point is validated:
   - The entry point resolves to a class that implements `Plugin`.
   - The `api_version` major matches `PLUGIN_API_VERSION`.
   - The allowlist (if configured) permits the plugin name.
   - The plugin is not in the denylist (`SCRAPER_PLUGINS_DENY`).
3. **Import**: The plugin module is imported, and the plugin class is instantiated.
   Each plugin runs in an isolated process (see the security model in [Plugins](plugins.md)).
4. **Registration**: The plugin's `register(registry, ctx)` method is called once.
5. **Failure**: The default policy (`SCRAPER_PLUGINS_ON_ERROR=fail`) aborts service startup
   if any plugin fails to load. With `SCRAPER_PLUGINS_ON_ERROR=warn`, the failed plugin is
   skipped with a warning log and the service starts.

## Provider types

### SearchProvider

Registers via `registry.register_search_provider(provider)`. The contract protocol
`SearchProvider` is re-exported from `app.search` (so is everything below):

```python
from app.search import (
    SearchProvider,
    SearchQuery,
    SearchResponse,
    SearchResult,
    SearchCapabilities,
    SearchMetadata,
    Vertical,
)


class MySearchProvider(SearchProvider):
    name = "my-search"

    async def capabilities(self) -> SearchCapabilities:
        # ``available`` is required; only advertise verticals you actually support.
        return SearchCapabilities(
            verticals=[Vertical.WEB],
            time_ranges=None,  # or a list of TimeRange you support
            supports_language=False,
            supports_time_range=False,
            available=True,
        )

    async def search(self, request: SearchQuery) -> SearchResponse:
        # Implement your search logic, then normalize every result into the
        # canonical shape and stamp metadata.provider with your own name.
        return SearchResponse(
            query=request.query,
            verticals=request.verticals,
            results=[
                SearchResult(
                    title="...",
                    url="https://example.com/...",
                    description="...",
                    source="my-search",
                    vertical=Vertical.WEB,
                )
            ],
            metadata=SearchMetadata(provider=self.name, elapsed_ms=0),
        )
```

Notes: `SearchResponse.metadata` is a `SearchMetadata` model (not a dict), and
`SearchResult` fields are validated, so map your backend payload into the exact canonical
fields before returning.

### TranscriptProvider

Registers via `registry.register_transcript_provider(provider)`.

```python
from app.transcript.contract import TranscriptHealth, TranscriptProvider
from app.transcript.models import TranscriptResponse, TranscriptSegment, TranscriptMetadata


class MyTranscriptProvider(TranscriptProvider):
    name = "my-transcript"

    async def health(self) -> TranscriptHealth:
        return TranscriptHealth(available=True, reason=None)

    async def fetch(self, video_id: str, languages: list[str]) -> TranscriptResponse:
        # ``languages`` is the ordered list of preferred caption languages (BCP-47).
        # Implement the fetch against your backend, then normalize:
        # - ``metadata.provider`` must be your own ``name``.
        # - Map backend payload into TranscriptSegment (start/duration/text in seconds).
        # - Report ``language`` / ``is_generated`` only when the backend provides them.
        # - Raise a transcript error from app.core.errors on failure.
        return TranscriptResponse(
            video_id=video_id,
            title=None,
            language=None,
            is_generated=None,
            provider=self.name,
            segments=[TranscriptSegment(start=0.0, duration=None, text="...")],
            text="...",
            total_words=1,
            metadata=TranscriptMetadata(provider=self.name, elapsed_ms=0),
        )
```

### AgentBrowserProvider

Registers via `registry.register_browser_provider(provider)`. This is a protocol from
`app.browser.providers.base` with `name`, `supports_evaluate`, and async
`create_session` / `close_session` / `create_tab` / `close_tab` / `evaluate` methods.
It is intended for interactive browser backends (Camofox is the built-in reference
implementation). Note that `AgentBrowserProvider` is not re-exported from `app.plugin_api`
in v1; import it from `app.browser.providers.base`.

### Extractor

Registers via `registry.register_extractor(result)` with an `ExtractionResult` from
`app.scraper.extractor_registry`:

```python
from app.scraper.extractor_registry import ExtractionResult

registry.register_extractor(
    ExtractionResult(
        domain="example.com",
        name="example-extractor",
        extractor_type="css",  # or "custom"
        fields={"title": {"selector": "h1", "type": "text"}},
    )
)
```

## PluginContext

The `PluginContext` provides a narrow, read-only view to plugins:

- `settings` — a frozen `SettingsView` with non-secret fields only (cache paths, log level,
  scraper/browser/security/proxy settings, server settings, ...). `api_key`, `api_keys`, and
  raw YAML blocks are excluded by construction.
- `logger` — a child logger named `scraper-api.plugins.<name>`.
- `http_client_factory` — a callable that returns the service's shared `httpx.AsyncClient`
  (closed at app shutdown).
- `per_plugin_env` — a dict of the plugin's `SCRAPER_PLUGIN_<NAME>_*` environment variables,
  with keys lowercased and underscores normalized to hyphens.

Plugins must not retain references to `ctx` after `register` returns.

## Configuration

There are two configuration channels for plugins:

1. **YAML `plugin:` section** — read via `ctx.settings.plugin` (a plain dict; use `.get()`
   with a default).
2. **Per-plugin environment variables** — `SCRAPER_PLUGIN_<NAME>_*`, where `<NAME>` is the
   plugin's name (dashes replaced by underscores, uppercased), read via `ctx.per_plugin_env`
   (a plain dict; use `.get()` with a default).

Example plugin named `my-plugin` with this config file and environment:

```yaml
# config.yaml
plugin:
  mode: fast
```

```bash
# Plugin receives this as ctx.per_plugin_env = {"api-key": "...", "timeout-seconds": "30"}
SCRAPER_PLUGIN_MY_PLUGIN_API_KEY="your-api-key"
SCRAPER_PLUGIN_MY_PLUGIN_TIMEOUT_SECONDS=30
```

In code:

```python
from app.plugin_api import Plugin, PluginContext, PluginRegistry


class MyPlugin(Plugin):
    name = "my-plugin"
    version = "0.1.0"
    api_version = "1.0.0"

    def register(self, registry: PluginRegistry, ctx: PluginContext) -> None:
        # YAML plugin section (config file)
        mode = ctx.settings.plugin.get("mode", "normal")
        # Per-plugin env vars (keys: lowercased, hyphenated)
        api_key = (ctx.per_plugin_env or {}).get("api-key")
        timeout = int((ctx.per_plugin_env or {}).get("timeout-seconds", "30"))
        debug = (ctx.per_plugin_env or {}).get("debug-enabled", "false") == "true"

        # Use config to construct providers
        registry.register_search_provider(MySearchProvider(api_key, timeout, debug, mode))
```

## Errors

Raise `app.core.errors.ScraperError` subclasses from provider methods:

- `SearchProviderUnavailableError`
- `SearchProviderTimeoutError`
- `InvalidProviderResponseError`
- `NoProviderAvailableError`
- `UnsupportedSearchCapabilityError`

For plugin loading failures, raise `app.plugins.errors.PluginLoadError` with clear messages.

## Versioning and compatibility

- The plugin API version is `PLUGIN_API_VERSION = "1.0.0"`.
- Plugins must declare `api_version` matching the **major** version.
- A plugin with a different major version is rejected at startup.

## Packaging and PyPI

### Naming convention

Use `scrapping-plugin-<slug>` for distribution names (e.g. `scrapping-plugin-example`).
The entry point *name* (not the distribution name) is the plugin name known to the loader
and allowlists.

### Entry point group

```toml
[project.entry-points."scrapping_service.plugins"]
# entry point name = plugin name; value = import path module:Class
example = "example_plugin.plugin:ExamplePlugin"
```

### Minimal pyproject.toml

This matches `examples/plugins/example/pyproject.toml` in this repository.

```toml
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "scrapping-plugin-example"
version = "0.1.0"
description = "A scrapping-service plugin"
readme = "README.md"
license = {text = "MIT"}
authors = [{name = "Your Name", email = "you@example.com"}]
classifiers = [
  "Development Status :: 3 - Alpha",
  "Intended Audience :: Developers",
  "License :: OSI Approved :: MIT License",
  "Programming Language :: Python :: 3",
  "Programming Language :: Python :: 3 :: Only",
  "Programming Language :: Python :: 3.11",
]
requires-python = ">=3.11"
# The plugin imports app.plugin_api at registration time. In development,
# ``pip install -e .`` the service first so app.* imports resolve.
# In the Docker base image the service is already installed, so leave this empty.
dependencies = []

[project.urls]
Homepage = "https://github.com/javiyt/scrapping-service"
Issues = "https://github.com/javiyt/scrapping-service/issues"

[tool.setuptools.packages.find]
where = ["."]
include = ["example_plugin*"]

[project.entry-points."scrapping_service.plugins"]
example = "example_plugin.plugin:ExamplePlugin"
```

## Testing

### Local development

Install a dev checkout of the service and your plugin:

```bash
pip install -e .                  # the service (makes app.* importable)
pip install -e examples/plugins/example
```

Then start the service normally and watch the logs for the plugin registration line.

### Docker-derived image

Follow `examples/plugins/example/Dockerfile`:

```dockerfile
FROM ghcr.io/javiyt/scrapping-service:latest
USER root
COPY . /tmp/example-plugin
RUN pip install --no-cache-dir /tmp/example-plugin
USER scraper
CMD ["python", "app/run.py"]
```

Build and run:

```bash
cd examples/plugins/example
docker build -t scrapping-service-example-plugin .
docker run --rm -p 8080:8080 scrapping-service-example-plugin
```

### Contract suites in CI

The reusable contract helpers live in `app.plugin_testing`:

```python
from app.plugin_testing import (
    assert_search_contract,
    run_search_contract_suite,
    assert_transcript_contract,
    run_transcript_contract_suite,
)

# Fails immediately with an assert message when the contract is broken
assert_search_contract(my_provider)
assert_transcript_contract(my_provider)

# As a pytest suite: each check is an individual test result
run_search_contract_suite(my_provider)
run_transcript_contract_suite(my_provider)
```

In a CI workflow:

```yaml
- name: Install example plugin into the service venv
  run: pip install -e examples/plugins/example

- name: Run the reusable contract suites against your provider
  run: pytest tests/test_example_plugin_v2.py -v
```

## Cookbook

### Async provider methods

Provider methods (`capabilities`, `search`, `health`, `fetch`) are `async`. Use `async def`
and `await` for async operations:

```python
async def search(self, request: SearchQuery) -> SearchResponse:
    async with self._http_client() as client:
        resp = await client.get(...)
        data = resp.json()
    return SearchResponse(...)
```

### Per-call timeouts

Implement timeouts inside your provider methods (the service's per-call isolation timeout
also applies on top of yours):

```python
import asyncio
from app.core.errors import SearchProviderTimeoutError


async def search(self, request: SearchQuery) -> SearchResponse:
    try:
        async with asyncio.timeout(5.0):
            # Perform search
            pass
    except TimeoutError:
        raise SearchProviderTimeoutError("Provider timed out")
```

Note: `asyncio.timeout` (Python 3.11+) raises the built-in `TimeoutError` on expiry.

### HTTP client

The context provides a shared HTTP client. Since `ctx` must not be retained after
`register`, capture the factory at registration time:

```python
def register(self, registry: PluginRegistry, ctx: PluginContext) -> None:
    client_factory = ctx.http_client_factory

    provider = MySearchProvider(http_client_factory=client_factory)
    registry.register_search_provider(provider)


async def search(self, request: SearchQuery) -> SearchResponse:
    client = self._client_factory()
    resp = await client.get(url)
    return SearchResponse(...)
```

### Debug logging

Use the context logger (captured at registration for the same reason):

```python
async def search(self, request: SearchQuery) -> SearchResponse:
    self._logger.debug("Starting search for %s", request.query)
    try:
        # ...
    except Exception as exc:
        self._logger.error("Search failed: %s", exc)
        raise
```
