# Example derived images for scrapping-service with plugins baked in

This directory contains example Docker images with plugins installed at build time.

## Example plugins

| Plugin | Location | Description |
|--------|----------|-------------|
| `scrapping-plugin-example` | `../examples/plugins/example/` | Full example demonstrating SearchProvider and per-domain extractors |
| `scrapping-test-plugin` | `scrapping-test-plugin/` | Minimal smoke-test fixture |

## Build and run

```bash
cd docs/examples/plugins
# Build the test-plugin image
docker build -t scrapping-service-plugin-example .
# Or build the example-plugin image
cd ../examples/plugins/example && docker build -t scrapping-service-example-plugin .
```

The images include:

- The official scrapping-service base image
- One or more plugins installed via pip
- All the original features (search, transcripts, browser, MCP)

See also:

- [Plugin Authoring Guide](https://github.com/javiyt/scrapping-service/blob/main/docs/plugins-authoring.md)
- [examples/templates/scrapping-plugin-cookiecutter](https://github.com/javiyt/scrapping-service/tree/main/examples/templates/scrapping-plugin-cookiecutter) for a cookiecutter template

## Using SCRAPER_PLUGINS instead

If you prefer dynamic installation at container start (no rebuild needed):

```yaml
services:
  scraper:
    image: ghcr.io/javiyt/scrapping-service:latest
    environment:
      SCRAPER_PLUGINS: "scrapping-plugin-example @ file:///path/to/example"
    volumes:
      - plugin-data:/app/plugins
volumes:
  plugin-data:
```

See `../plugins.md` for when to choose each approach.

## Pinning the base image

For reproducible builds, pin the base image by digest:

```dockerfile
FROM ghcr.io/javiyt/scrapping-service@sha256:...
```

Get the digest with:

```bash
docker pull ghcr.io/javiyt/scrapping-service:latest
docker inspect ghcr.io/javiyt/scrapping-service:latest --format='{{index .RepoDigests 0}}'
```

Or use the label embedded in the image:

```bash
docker inspect ghcr.io/javiyt/scrapping-service:latest --format='{{index .Config.Labels "org.scrapping-service.plugin-api-version"}}'
```