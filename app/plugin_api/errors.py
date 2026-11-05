"""Errors raised by the plugin loader.

This module defines exception types used during plugin loading and
registration.

"""

from __future__ import annotations

from typing import Any


class PluginLoadError(Exception):
    """Raised when a plugin cannot be loaded or registered.

    The loader aborts startup on any plugin error (fail-fast per ADR 0001).
    This exception wraps more specific errors and includes the plugin
    name and a human-readable cause.

    Attributes:
        plugin_name: Name of the plugin that failed.
        cause: Original exception or error message.
        extra: Optional extra context (e.g., api_version mismatch).

    """

    def __init__(self, plugin_name: str, cause: str, extra: dict[str, Any] | None = None) -> None:
        super().__init__(f"Plugin '{plugin_name}': {cause}")
        self.plugin_name = plugin_name
        self.cause = cause
        self.extra = extra or {}

    @property
    def to_dict(self) -> dict[str, Any]:
        """Return a serializable dict representation."""
        return {
            "error": "plugin_load_error",
            "plugin_name": self.plugin_name,
            "cause": self.cause,
            **self.extra,
        }

    @classmethod
    def for_api_version_mismatch(
        cls,
        plugin_name: str,
        plugin_api_version: str,
        expected_api_version: str,
    ) -> PluginLoadError:
        """Create a PluginLoadError for API version mismatch."""
        return cls(
            plugin_name,
            f"api_version '{plugin_api_version}' does not match expected '{expected_api_version}'",
            {
                "plugin_api_version": plugin_api_version,
                "expected_api_version": expected_api_version,
            },
        )

    @classmethod
    def for_provider_collision(cls, plugin_name: str, provider_name: str) -> PluginLoadError:
        """Create a PluginLoadError for provider name collision."""
        return cls(
            plugin_name,
            f"provider '{provider_name}' already registered",
            {"provider_name": provider_name},
        )

    @classmethod
    def for_import_failure(cls, plugin_name: str, exc: Exception) -> PluginLoadError:
        """Create a PluginLoadError for import error."""
        return cls(
            plugin_name,
            f"import error: {exc}",
            {"exception": str(exc)},
        )

    @classmethod
    def for_registration_failure(
        cls, plugin_name: str, provider_name: str, exc: Exception
    ) -> PluginLoadError:
        """Create a PluginLoadError for registration error."""
        return cls(
            plugin_name,
            f"failed to register provider '{provider_name}': {exc}",
            {"provider_name": provider_name, "exception": str(exc)},
        )
