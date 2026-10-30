"""Tests for the search provider factory (``build_search_providers``)."""

from app.core.config import Settings
from app.search import SearchProvider
from app.search.providers import SearxngProvider, build_search_providers


def _settings_with_search_block(search_block: dict | None) -> Settings:
    settings = Settings(api_key="test-key", server_api_key_required=False)
    object.__setattr__(settings, "raw_yaml_search", search_block)
    return settings


class TestBuildSearchProviders:
    def test_no_search_block_no_providers(self):
        providers = build_search_providers(_settings_with_search_block(None))
        assert providers == []

    def test_disabled_searxng_no_providers(self):
        settings = _settings_with_search_block({"providers": {"searxng": {"enabled": False}}})
        assert build_search_providers(settings) == []

    async def test_enabled_searxng_builds_provider(self):
        settings = _settings_with_search_block(
            {
                "providers": {
                    "searxng": {
                        "enabled": True,
                        "base_url": "http://searxng:8080",
                    }
                }
            }
        )
        providers = build_search_providers(settings)
        assert len(providers) == 1
        provider = providers[0]
        assert isinstance(provider, SearxngProvider)
        assert isinstance(provider, SearchProvider)
        assert provider.name == "searxng"
        await provider.close()

    def test_empty_providers_mapping(self):
        assert build_search_providers(_settings_with_search_block({"providers": {}})) == []

    def test_enabled_without_base_url_raises(self):
        settings = _settings_with_search_block({"providers": {"searxng": {"enabled": True}}})
        try:
            build_search_providers(settings)
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for enabled provider without base_url")
