# Deployment

## Docker Image

The Docker workflow publishes multi-architecture images to GitHub Container
Registry on pushes to `main`, version tags, and manual dispatches.

Published image:

```text
ghcr.io/javiyt/scrapping-service
```

## Raspberry Pi Deploy

The deploy script can pull a pre-built image from GHCR and update the remote
Quadlet service.

```bash
./scripts/deploy.sh javiyt@raspberry5 --tag latest --with-env
```

For a release tag:

```bash
./scripts/deploy.sh javiyt@raspberry5 --tag v1.1.0 --with-env
```

## GitHub Pages

Documentation is deployed by `.github/workflows/pages.yml` after a successful
build from the `main` branch.

The published site is:

```text
https://javiyt.github.io/scrapping-service/
```
