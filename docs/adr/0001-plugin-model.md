# ADR 0001: Plugin Model

## Context

The scrapping-service needs to support third-party extensions without requiring modifications to the core service code. Users should be able to add new search providers, transcript providers, browser backends, or domain-specific extractors/normalizers through external packages.

Currently, the service provides a clear contract for providers via Python `Protocol` types:

- `SearchProvider` (`app/search/contract.py`)
- `TranscriptProvider` (`app/transcript/contract.py`)
- `AgentBrowserProvider` (`app/browser/providers/base.py`)

The registry patterns (`SearchProviderRegistry`, `TranscriptProviderRegistry`) allow the service to look up providers by name and build ordered fallback chains.

However, there is no standard mechanism for external packages to extend the service. This ADR defines the plugin model that third-party packages must follow to integrate with scrapping-service.

## Decision

The plugin system will use **Python entry points** and **in-process execution**:

1. A plugin is a regular pip-installable Python package that exposes one or more entry points in the group `scrapping_service.plugins`.
2. The service discovers plugins at startup by scanning `scrapping_service.plugins` via `importlib.metadata`.
3. Plugins run in-process within the same Python process/container as the scrapping-service.

## Consequences

### In-Process Execution (Trusted Code)

Plugins execute in the same process as the scrapping-service. This has important implications:

- **Privileges**: Plugins run with the same privileges as the service container. A compromised or malicious plugin can access all resources the service has (internal services, cache files, API keys, etc.).
- **Performance**: No serialization overhead or network calls; direct function calls between plugin and core service.
- **Simplicity**: The plugin contract is a Python interface (Protocols); no HTTP protocol or JSON serialization to maintain.

**Why not out-of-process (HTTP) plugins?**  
Out-of-process execution (HTTP-based plugin service) would add significant complexity and risk:

- **Latency**: Every plugin invocation becomes an HTTP round-trip, increasing latency for each search/transcript/browser operation.
- **Network reliability**: Network failures, timeouts, and connection pooling concerns would need to be handled at the plugin layer.
- **Security surface**: Exposing a plugin HTTP API introduces authentication/authorization and SSRF risks at the plugin boundary.
- **Contract complexity**: An HTTP protocol would need to mirror the synchronous Python protocol, doubling the maintenance surface.

For v1, the service will **reject and defer** out-of-process (HTTP) plugin models. If there is a strong use case in the future, it can be revisited with explicit threat modeling and performance trade-off analysis.

## Architecture

### Packaging

A plugin is a Python package with:

- A `setup.py` / `pyproject.toml` defining entry points:

```toml
[project]
name = "my-custom-search"
version = "1.0.0"

[project.entry-points."scrapping_service.plugins"]
my_search = "my_search.plugin:MySearchPlugin"
```

- A `PLUGIN_API_VERSION` constant that matches the service's expected version.
- Implementation of the `Plugin` base class (see Contract section below).

### Installation Paths

Two installation methods are supported:

#### 1. Container Start (Dynamic)

Set the `SCRAPER_PLUGINS` environment variable to a comma-separated list of package specifiers:

```bash
SCRAPER_PLUGINS="my-custom-search>=1.0.0,another-plugin==2.1.0"
```

At startup, the service:

1. Reads `SCRAPER_PLUGINS`
2. Runs `pip install --quiet <packages>` to install them
3. Discovers and loads plugins via entry points

This model is convenient for development and simple deployments.

#### 2. Derived Image (Static)

Create a custom Docker image:

```dockerfile
FROM ghcr.io/javiyt/scrapping-service:latest
RUN pip install my-custom-search>=1.0.0 another-plugin==2.1.0
```

This model is recommended for production because:

- The plugin versions are versioned in the Dockerfile (reproducible builds)
- No runtime `pip install` dependency on external indexes
- CI/CD can build and test the derived image before deployment

### Execution Model

Plugins are loaded during the application lifespan (startup), before routers are built:

1. Load `SCRAPER_PLUGINS` from environment (if set) and trigger installation
2. Load all entry points from `scrapping_service.plugins`
3. Validate each plugin's `api_version` matches the expected version
4. Check each plugin against the allowlist (if enabled)
5. Instantiate the plugin and call `register(registry, ctx)`
6. The plugin registers its providers into the service's existing registries

### Extension Points (v1)

The following extension points are available in v1:

| Extension Point | Contract | Registry | Use Case |
|-----------------|----------|----------|----------|
| Search Provider | `SearchProvider` (name, capabilities, search) | `SearchProviderRegistry` | Custom search backends |
| Transcript Provider | `TranscriptProvider` (name, health, fetch) | `TranscriptProviderRegistry` | Alternative transcript sources |
| Browser Provider | `AgentBrowserProvider` | `BrowserBackendFactory` | New browser backends |
| Per-Domain Extractor | `Extractor` (to be defined) | Domain policy | Domain-specific HTML extraction |
| Per-Domain Normalizer | `Normalizer` (to be defined) | Domain policy | Domain-specific HTML cleanup |

Plugins **cannot** add arbitrary FastAPI routes or MCP tools in v1. These capabilities require:

- Extensive security review (auth bypass, SSRF, permission escalation)
- OpenAPI schema growth management
- Plugin signing/verification infrastructure

Custom routes and MCP tools are deferred to post-v1.

### Plugin Contract

A plugin must implement:

```python
# app/plugin_api/__init__.py (to be created in #171)
from typing import Protocol, runtime_checkable
from dataclasses import dataclass

@dataclass(frozen=True)
class PluginContext:
    """Read-only context passed to plugins during registration."""
    settings: Settings  # Subset for plugin-specific configuration
    logger: Logger
    http_client_factory: Callable[[], AsyncClient]  # Reusable client

class PluginRegistry(Protocol):
    """Registry into which plugins register their providers."""
    def register_search_provider(self, provider: SearchProvider) -> None: ...
    def register_transcript_provider(self, provider: TranscriptProvider) -> None: ...
    def register_browser_provider(self, provider: AgentBrowserProvider) -> None: ...

@runtime_checkable
class Plugin(Protocol):
    """Base protocol every plugin must satisfy."""
    
    name: str
    version: str
    api_version: str  # Must match PLUGIN_API_VERSION
    
    def register(self, registry: PluginRegistry, ctx: PluginContext) -> None:
        """Register providers with the service."""
        ...
```

### Compatibility Policy

- `PLUGIN_API_VERSION` uses semantic versioning (MAJOR.MINOR.PATCH).
- The loader enforces **major version matching**: `plugin.api_version.major == SERVICE_API_VERSION.major`.
- Minor and patch versions are advisory only (plugins may use features from later minor versions if they are backward-compatible).
- Incompatible plugins (wrong major version) cause container startup to fail with a clear error message.

### Trust and Security Model

#### Allowlist

Set `SCRAPER_PLUGIN_ALLOWLIST_ENABLED=true` to enable allowlist mode. Only plugins listed in `SCRAPER_PLUGIN_ALLOWLIST` (comma-separated) are allowed:

```bash
SCRAPER_PLUGIN_ALLOWLIST=my-custom-search,trusted-analytics
```

If allowlist is disabled (default for v1), all entry points in `scrapping_service.plugins` are loaded.

#### Version Pinning

The `SCRAPER_PLUGINS` environment variable should use pinned versions:

```bash
SCRAPER_PLUGINS="my-custom-search==1.2.3"
```

The derived image approach (Dockerfile `RUN pip install`) naturally pins versions.

#### Hash Verification (Optional)

Future versions may add optional hash verification:

```bash
SCRAPER_PLUGIN_HASHES="my-custom-search:sha256:abc123..."
```

This would verify package integrity during installation.

#### Install Failure Behavior

If plugin installation or loading fails:

1. Log the failure with full error details (including stack trace).
2. **Abort container startup** (fail-fast).
3. Exit with non-zero code so orchestration (Docker, Kubernetes) can restart or alert.

This ensures a broken plugin does not silently degrade the service.

## Alternatives Considered

### Alternative 1: Out-of-Process (HTTP) Plugins

**Rejected** for v1. See "Consequences" section for full rationale.

### Alternative 2: Embedded Scripting (e.g., Lua, WASM)

**Rejected**. Adds complexity without solving the core requirement. Most scraping logic is Python-specific (httpx, selenium, beautifulsoup4). WASM would require cross-compilation and has limited ecosystem integration.

### Alternative 3: Monorepo with Symlinked Extensions

**Rejected**. Defeats the purpose of extensibility; users cannot add third-party extensions without forking the monorepo.

### Alternative 4: Dynamic `sys.path` Modification

**Rejected**. Unpredictable import behavior, no version isolation, conflicts with project dependencies. Entry points are the standard Python mechanism.

## References

- Ticket #171: Add versioned plugin API and entry-point loader
- Ticket #172: Install plugins automatically at container start via SCRAPER_PLUGINS
- Ticket #173: Support and document derived Docker images with plugins baked in
- Ticket #174: Add allowlist, failure isolation and security model for plugins
- Ticket #175: Provide example plugin, authoring guide and plugin contract tests
- Ticket #176: Add health, metrics, CLI and docs for plugins
- Ticket #177: Support installing plugins in a plain Python environment