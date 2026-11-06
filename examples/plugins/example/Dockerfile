# syntax=docker/dockerfile:1
#
# Example derived image for scrapping-service with the example plugin baked in.
# Compare with docs/examples/plugins/Dockerfile which uses test_plugin.

FROM ghcr.io/javiyt/scrapping-service:latest

# Install the example plugin at build time (baked-in, reproducible).
USER root
COPY example /tmp/example
RUN pip install --no-cache-dir /tmp/example
USER scraper

# Optional: allowlist plugins (empty = load all; plugin is already discovered
# because it's installed in the same Python environment as the base image).
# ENV SCRAPER_PLUGIN_ALLOWLIST=example-plugin

CMD ["python", "app/run.py"]