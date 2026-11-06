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

## Network requirements

Static network access depends on your plugin sources:

- **PyPI default index**: Requires outbound HTTPS to `pypi.org`.
- **Custom index**: Requires outbound HTTPS to your index.
- **VCS URLs**: Requires outbound access to the VCS server (GitHub, GitLab, etc.).
- **Local wheelhouse** (offline mode): No network required; all wheels must be
  pre-downloaded to the mounted wheelhouse directory.

For offline deployments, pre-build wheels:

```bash
mkdir wheels
pip wheel --no-deps -w wheels my-package==1.0.0
```

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

Set the `SCRAPER_PLUGINS` environment variable to a comma or space-separated list
of pip requirement specifiers:

```bash
SCRAPER_PLUGINS="my-custom-search>=1.0.0,another-plugin==2.1.0"
```

The container installs the specified packages at startup, then loads plugins
via their entry points. The install step:

- Runs **before** uvicorn starts.
- Runs as the unprivileged `scraper` user.
- Installs into an isolated `/app/plugins` directory.
- Applies constraints from `/app/requirements.txt` so plugins cannot override
core dependencies.
- Stores a fingerprint to skip re-installation on restarts when requirements
haven't changed.
- Supports `SCRAPER_PLUGINS_ON_ERROR=fail|warn` (default `fail`).

#### Environment variables

| Variable | Description | Default |
|----------|-------------|---------|
| `SCRAPER_PLUGINS` | Comma/space-separated pip requirement specifiers | (empty) |
| `SCRAPER_PLUGINS_DIR` | Plugin installation directory | `/app/plugins` |
| `SCRAPER_PLUGINS_CONSTRAINTS` | Constraints file path | `/app/requirements.txt` |
| `SCRAPER_PLUGINS_INDEX_URL` | Primary PyPI index URL | (none) |
| `SCRAPER_PLUGINS_EXTRA_INDEX_URL` | Extra PyPI index URL | (none) |
| `SCRAPER_PLUGINS_REQUIREMENTS_FILE` | Path to requirements.txt file (supports --hash) | (none) |
| `SCRAPER_PLUGINS_OFFLINE` | Install from wheelhouse only (`true`/`false`) | `false` |
| `SCRAPER_PLUGINS_WHEELHOUSE` | Local wheelhouse directory for offline mode | `/plugins/wheels` |
| `SCRAPER_PLUGINS_ON_ERROR` | Failure policy (`fail` or `warn`) | `fail` |

#### Examples

**Basic usage**
```bash
docker run -e SCRAPER_PLUGINS="my-custom-search>=1.0.0,another-plugin==2.1.0" scrapping-service
```

**Custom index URL**
```bash
docker run -e SCRAPER_PLUGINS="my-package==1.0.0" \
  -e SCRAPER_PLUGINS_INDEX_URL="https://pypi.example.com/simple" \
  scrapping-service
```

**Offline installation from wheelhouse**
```bash
docker run -v ./wheels:/plugins/wheels \
  -e SCRAPER_PLUGINS="my-plugin-1.0.0-py3-none-any.whl" \
  -e SCRAPER_PLUGINS_OFFLINE=true \
  scrapping-service
```

**Docker Compose**
```yaml
services:
  scraper:
    image: ghcr.io/javiyt/scrapping-service:latest
    environment:
      SCRAPER_PLUGINS: "my-custom-search>=1.0.0,another-plugin==2.1.0"
      SCRAPER_PLUGINS_DIR: "/app/plugins"
    volumes:
      - plugin-data:/app/plugins
volumes:
  plugin-data:
```

Persisting `/app/plugins` as a volume ensures installs survive container
restarts; unchanged requirements will skip re-installation on next startup.

### 2. Derived image (static)

Create a custom Dockerfile:

```dockerfile
FROM ghcr.io/javiyt/scrapping-service:latest
RUN pip install my-custom-search>=1.0.0 another-plugin==2.1.0
```

Recommended for production (reproducible builds, no runtime pip).

## Security considerations

Plugins execute in the same process as the scrapping-service with the same
privileges. Only install trusted packages from verified sources.

The plugin installation uses constraints from `requirements.txt` to prevent
plugins from overriding core dependencies, but plugins can still affect the
service behavior through their registered providers.

## Cold-start cost

Dynamic plugin installation adds to container startup time:

- **First start**: Full `pip install` run plus entry-point discovery.
- **Subsequent starts with unchanged requirements**: Cache hit, no reinstall.
- **Offline installs**: Fastest (no network round-trips).

Typical cold-start overhead:
- Online install: 5-30 seconds depending on network and package count.
- Offline install: 1-5 seconds (wheelhouse read from volume).

For strict performance requirements, prefer the derived-image approach.

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