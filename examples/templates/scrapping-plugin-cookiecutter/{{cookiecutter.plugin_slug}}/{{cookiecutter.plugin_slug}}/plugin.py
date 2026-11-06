"""{{ cookiecutter.entry_point_name }} plugin for scrapping-service.

This plugin implements the scrapping-service plugin API v1.
See https://github.com/javiyt/scrapping-service for details.

Usage:

    from app.plugin_api import Plugin, PluginContext, PluginRegistry

    class My{{ cookiecutter.entry_point_name.capitalize().replace('-', '_') }}(Plugin):
        name = "{{ cookiecutter.entry_point_name }}"
        version = "{{ cookiecutter.version }}"
        api_version = "1.0.0"

        def register(self, registry: PluginRegistry, ctx: PluginContext) -> None:
            # Register your providers here
            pass
"""

from __future__ import annotations

from typing import Any

from app.plugin_api import Plugin, PluginContext, PluginRegistry


class {{ cookiecutter.entry_point_name.capitalize().replace('-', '_') }}(Plugin):
    """{{ cookiecutter.project_name }} plugin."""

    name = "{{ cookiecutter.entry_point_name }}"
    version = "{{ cookiecutter.version }}"
    api_version = "1.0.0"

    def register(self, registry: PluginRegistry, ctx: PluginContext) -> None:
        """Register providers with the service."""
        # TODO: Implement provider registration
        pass