# Example derived image for scrapping-service with a plugin baked in.

FROM ghcr.io/javiyt/scrapping-service:latest

# Install a test plugin at build time (baked-in, reproducible).
# For dynamic installation at container start, use SCRAPER_PLUGINS instead.
USER root
COPY test_plugin /tmp/test_plugin
RUN pip install --no-cache-dir /tmp/test_plugin
USER scraper

# Optional: allowlist plugins (empty = load all; plugin is already discovered
# because it's installed in the same Python environment as the base image).
# ENV SCRAPER_PLUGIN_ALLOWLIST=test-plugin

CMD ["python", "app/run.py"]