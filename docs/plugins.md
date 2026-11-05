# Plugins

Scrapping-service supports third-party extensions through a versioned plugin
API and entry-point loader.

## What is a plugin?

A plugin is a Python package that registers one or more providers into the
service at startup via Python [entry points] in the group
`scrapping_service.plugins`.

[entry points]: https://packaging.python.org/en/latest/specifications/entry-points/

Plugins are intended for:

- Custom search providers (beyond SearXNG and Camofox).
- Custom transcript providers (beyond YouTube Transcript API and yt-dlp).
- Custom browser backends (beyond Camofox).
- Custom per-domain extractors (to run on scrape responses).

Plugins **cannot** add arbitrary FastAPI routes or MCP tools in v1; those
capabilities require security review and are deferred to post-v1.

## Plugin contract

A plugin must implement:

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

The `PLUGIN_API_VERSION` is `"1.0.0"`; the loader enforces major-version
compatibility (plugins with `api_version.major != 1` are rejected at startup).

The `PluginContext` provides a **narrow** view of settings:

- Non-secret configuration fields only.
- Logger (`scraper-api.plugins.<name>`).
- HTTP client factory (`http_client_factory()` → `httpx.AsyncClient`).

Plugins must never access secrets (no `api_key`, `api_keys`, or raw YAML
blocks in the context).

## Entry-point packaging

A plugin package declares the entry point in `setup.py` or `pyproject.toml`:

```toml
[project]
name = "my-custom-search"
version = "1.0.0"

[project.entry-points."scrapping_service.plugins"]
my_search = "my_search.plugin:MySearchPlugin"
```

## Loading strategies

### 1. Container start (dynamic)

Set `SCRAPER_PLUGINS` environment variable:

```bash
SCRAPER_PLUGINS="my-custom-search>=1.0.0,another-plugin==2.1.0"
```

The loader installs packages and loads plugins at startup.

### 2. Derived image (static)

Create a custom Dockerfile:

```dockerfile
FROM ghcr.io/javiyt/scrapping-service:latest
RUN pip install my-custom-search>=1.0.0 another-plugin==2.1.0
```

Recommended for production (reproducible builds, no runtime pip).

## Configuration

The `plugin:` block in `configs/config.yaml` controls loading:

```yaml
plugin:
  enabled: true       # Load plugins on startup
  allowlist: []       # If non-empty, only allowlisted plugins are loaded
```

Or via environment:

- `SCRAPER_PLUGIN_ENABLED=true`
- `SCRAPER_PLUGIN_ALLOWLIST=my-custom-search,trusted-analytics`

## Known limitations

- Plugin-provided providers receive the **global** settings (not per-profile
  overrides); plugin configuration is read once at startup.
- Browser provider registration conflicts with built-in provider; only one
  browser provider can be active at a time (plugin or built-in).
- Extractors run only for scrape responses without an explicit `extract`
  block (per-request `extract` takes precedence).

For full authoring guide and examples, see ticket #175.