"""Application configuration management.

Settings are loaded from environment variables with YAML config file as base.
Priority (highest to lowest):
  1. Environment variables (SCRAPER_* prefix)
  2. YAML config file (configs/config.yaml)
  3. Default values
"""

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings.

    Environment variables take priority over YAML config values.
    Use the ``SCRAPER_`` prefix, e.g.:
    - ``SCRAPER_SERVER_HOST=0.0.0.0``
    - ``SCRAPER_SERVER_PORT=8080``
    - ``SCRAPER_CACHE_SQLITE_PATH=/custom/path/cache.db``
    """

    model_config = SettingsConfigDict(
        env_prefix="SCRAPER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ Server
    server_host: str = "0.0.0.0"
    server_port: int = 8080
    server_api_key_required: bool = True
    server_cors_origins: list[str] = Field(default_factory=lambda: ["*"])

    # ------------------------------------------------------------- Auth / Keys
    scraper_api_key: str = "change-me"

    # ---------------------------------------------------------------- Cache DB
    cache_backend: str = "sqlite"
    cache_sqlite_path: str = "data/scraper-cache.db"
    cache_default_ttl_seconds: int = 21600  # 6 hours
    cache_max_html_size_mb: int = 10
    cache_stale_if_error: bool = True

    # ------------------------------------------------------------- Cache cleanup
    cache_cleanup_enabled: bool = True
    cache_cleanup_interval_seconds: int = 3600
    cache_delete_expired_after_seconds: int = 86400  # 24 hours
    cache_max_entries: int = 10000
    cache_max_size_mb: int = 512
    cache_vacuum_after_cleanup: bool = False

    # ------------------------------------------------------------- Scraper
    scraper_default_mode: str = "auto"
    scraper_headless: bool = True
    scraper_timeout_seconds: int = 45
    scraper_max_concurrency: int = 1
    scraper_user_agent_profile: str = "desktop_es"

    # ------------------------------------------------------------- Browser
    browser_arguments: list[str] = Field(
        default_factory=lambda: [
            "--headless=new",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-background-networking",
            "--window-size=1366,768",
        ]
    )

    # ------------------------------------------------------------- Security
    security_block_private_ips: bool = True
    security_block_localhost: bool = True
    security_allowed_domains: list[str] = Field(default_factory=list)

    # Domain-specific overrides loaded from YAML.
    domains: dict[str, dict[str, Any]] = Field(default_factory=dict)

    # ------------------------------------------------------------- Debug
    debug_screenshots: bool = False
    debug_html_dumps: bool = False
    debug_dir: str = "debug"

    # ------------------------------------------------------------- Runtime
    config_path: str = Field(default="", alias="CONFIG_PATH")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # ------------------------------------------------------------------ validators

    @field_validator("server_port")
    @classmethod
    def _validate_port(cls, v: int) -> int:
        if not 1 <= v <= 65535:
            raise ValueError(f"Port must be in range 1-65535, got {v}")
        return v

    @field_validator("cache_max_html_size_mb")
    @classmethod
    def _validate_max_html_size(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"max_html_size_mb must be >= 1, got {v}")
        return v

    @field_validator("scraper_default_mode")
    @classmethod
    def _validate_mode(cls, v: str) -> str:
        if v not in ("http", "browser", "auto"):
            raise ValueError(f"Invalid scraper mode '{v}'; must be one of: http, browser, auto")
        return v

    # ------------------------------------------------------------------ loading

    @classmethod
    def load(cls, config_path: str = "") -> "Settings":
        """Load settings from YAML config file with environment-variable overrides.

        Args:
            config_path: Path to YAML config file.  If empty, the env var
                ``CONFIG_PATH`` (or ``SCRAPER_CONFIG_PATH``) is used.

        Priority (highest to lowest):
          1. Environment variables (SCRAPER_*)
          2. YAML config file
          3. Hard-coded defaults

        Returns a validated :class:`Settings` instance.
        """
        if not config_path:
            config_path = os.getenv("CONFIG_PATH", "")

        merged: dict[str, Any] = {}

        # 1. YAML values (base).
        if config_path:
            config_file = Path(config_path)
            if config_file.exists():
                with open(config_file, encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                merged.update(cls._map_yaml_to_fields(data))

        # 2. Env overrides (highest priority) — pydantic coerces types.
        for field_name in cls.model_fields:
            env_key = f"SCRAPER_{field_name.upper()}"
            if env_key in os.environ:
                merged[field_name] = os.environ[env_key]

        # Non-prefixed env vars.
        for env_key, field_name in [("CONFIG_PATH", "config_path"), ("LOG_LEVEL", "log_level")]:
            if env_key in os.environ and field_name in cls.model_fields:
                merged[field_name] = os.environ[env_key]

        return cls(**merged)

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _map_yaml_to_fields(data: dict) -> dict[str, Any]:
        """Map nested YAML structure to flat field names expected by this model."""
        section_mapping = {
            "server": "server",
            "cache": "cache",
            "scraper": "scraper",
            "browser": "browser",
            "security": "security",
            "debug": "debug",
        }
        result: dict[str, Any] = {}

        for yaml_key, field_prefix in section_mapping.items():
            if yaml_key in data and isinstance(data[yaml_key], dict):
                for k, v in data[yaml_key].items():
                    result[f"{field_prefix}_{k}"] = v

        # Domains stay as a dictionary.
        if "domains" in data and isinstance(data["domains"], dict):
            result["domains"] = data["domains"]

        # Top-level aliases
        if "log_level" in data:
            result["log_level"] = data["log_level"]

        return result

    def get_domain_config(self, domain: str) -> dict[str, Any]:
        """Return domain-specific overrides for *domain*."""
        return self.domains.get(domain, {})

    def get_domain_ttl(self, domain: str) -> int:
        """Return the default TTL configured for *domain*, or the global default."""
        cfg = self.domains.get(domain, {})
        return cfg.get("default_ttl_seconds", self.cache_default_ttl_seconds)

    def is_domain_allowed(self, domain: str) -> bool:
        """Check whether *domain* is explicitly allowed."""
        if not self.security_allowed_domains:
            return True
        return domain in self.security_allowed_domains

    def all_domain_names(self) -> list[str]:
        """Return list of allowed domain names from config and policies."""
        if self.security_allowed_domains:
            return self.security_allowed_domains
        return list(self.domains.keys())
