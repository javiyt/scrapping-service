# Example derived image for scrapping-service with plugins baked in

This example shows how to build a custom image on top of the official
scrapping-service image with plugins installed at build time.

## Build and run

```bash
cd docs/examples/plugins
docker build -t scrapping-service-plugin-example .
docker run -p 8080:8080 scrapping-service-plugin-example
```

The `scrapping-service-plugin-example` image includes:

- The official scrapping-service base image
- A test plugin installed via pip
- All the original features (search, transcripts, browser, MCP)

## Using SCRAPER_PLUGINS instead

If you prefer dynamic installation at container start (no rebuild needed):

```yaml
services:
  scraper:
    image: ghcr.io/javiyt/scrapping-service:latest
    environment:
      SCRAPER_PLUGINS: "scrapping-test-plugin @ file:///path/to/test_plugin"
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