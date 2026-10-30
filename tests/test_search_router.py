"""Tests for the search provider registry, router and routing configuration.

All routing behaviour is exercised with in-memory fake providers (the
:class:`app.search.SearchProvider` protocol is structural), never with real
backends.  Coverage follows the ticket:

- first provider selected (auto)
- explicit provider (no silent fallback)
- disabled provider
- capability mismatch (strict vs degradable)
- transient fallback (deterministic)
- no eligible provider
- no fallback on invalid request
- configured provider order
- response metadata (attempts, fallback flag, warnings, secret redaction)
"""

import asyncio

import pytest

from app.core.config import Settings
from app.core.errors import (
    InvalidProviderResponseError,
    NoProviderAvailableError,
    SearchProviderTimeoutError,
    SearchProviderUnavailableError,
    UnsupportedSearchCapabilityError,
    ValidationError,
)
from app.search import (
    SearchProvider,
    SearchProviderRegistry,
    SearchRouter,
    SearchRouterConfig,
    build_search_router,
)
from app.search.models import (
    SearchCapabilities,
    SearchMetadata,
    SearchQuery,
    SearchResponse,
    SearchResult,
    TimeRange,
    Vertical,
)
from app.search.router import redact_secrets


# ==================================================================== fakes
class FakeProvider:
    """In-memory provider satisfying the SearchProvider protocol."""

    def __init__(
        self,
        name: str = "fake",
        available: bool = True,
        verticals: list[Vertical] | None = None,
        time_ranges: list[TimeRange] | None = None,
        supports_language: bool = False,
        supports_safe_search: bool = False,
        supports_paging: bool = False,
        supports_source_selection: bool = False,
        max_results: int | None = None,
        search_error: Exception | None = None,
        capability_error: Exception | None = None,
        capability_delay: float = 0.0,
    ) -> None:
        self._name = name
        self._available = available
        self._verticals = verticals or [Vertical.WEB]
        self._time_ranges = time_ranges or []
        self._supports_language = supports_language
        self._supports_safe_search = supports_safe_search
        self._supports_paging = supports_paging
        self._supports_source_selection = supports_source_selection
        self._max_results = max_results
        self._search_error = search_error
        self._capability_error = capability_error
        self._capability_delay = capability_delay
        self.search_calls = 0
        self.capability_calls = 0

    @property
    def name(self) -> str:
        return self._name

    async def capabilities(self) -> SearchCapabilities:
        self.capability_calls += 1
        if self._capability_delay:
            await asyncio.sleep(self._capability_delay)
        if self._capability_error is not None:
            raise self._capability_error
        return SearchCapabilities(
            verticals=self._verticals,
            time_ranges=self._time_ranges,
            supports_language=self._supports_language,
            supports_safe_search=self._supports_safe_search,
            supports_paging=self._supports_paging,
            supports_source_selection=self._supports_source_selection,
            max_results=self._max_results,
            available=self._available,
        )

    async def search(self, request: SearchQuery) -> SearchResponse:
        self.search_calls += 1
        if self._search_error is not None:
            raise self._search_error
        return SearchResponse(
            query=request.query,
            verticals=request.verticals,
            results=[
                SearchResult(
                    title=f"Result from {self._name}",
                    url="https://example.com/1",
                    rank=1,
                    vertical=request.verticals[0],
                    provider=self._name,
                )
            ],
            metadata=SearchMetadata(provider=self._name, providers_attempted=[self._name]),
        )


def capable(name: str = "fake", **overrides) -> FakeProvider:
    """A fully eligible provider for the default request (web + moderate)."""
    defaults: dict = {"name": name, "verticals": [Vertical.WEB], "supports_safe_search": True}
    defaults.update(overrides)
    return FakeProvider(**defaults)  # type: ignore[arg-type]


def query(**overrides) -> SearchQuery:
    data: dict = {"query": "dolphins"}
    data.update(overrides)
    return SearchQuery(**data)


def make_router(
    providers: list[FakeProvider],
    **config_kwargs,
) -> SearchRouter:
    return SearchRouter(SearchProviderRegistry(providers), SearchRouterConfig(**config_kwargs))


def _settings_with_search_block(search_block: dict | None) -> Settings:
    settings = Settings(api_key="test-key", server_api_key_required=False)
    object.__setattr__(settings, "raw_yaml_search", search_block)
    return settings


# ==================================================================== registry
class TestRegistry:
    def test_registration_order_is_preserved(self):
        registry = SearchProviderRegistry([capable("a"), capable("b"), capable("c")])
        assert registry.names == ["a", "b", "c"]
        assert [p.name for p in registry.all()] == ["a", "b", "c"]
        assert [p.name for p in registry] == ["a", "b", "c"]
        assert len(registry) == 3

    def test_empty_registry(self):
        registry = SearchProviderRegistry()
        assert registry.names == []
        assert registry.all() == []
        assert len(registry) == 0

    def test_get_and_contains(self):
        registry = SearchProviderRegistry([capable("a")])
        assert registry.get("a") is not None
        assert registry.get("missing") is None
        assert "a" in registry
        assert "missing" not in registry

    def test_duplicate_name_raises(self):
        registry = SearchProviderRegistry([capable("a")])
        with pytest.raises(ValueError, match="already registered"):
            registry.register(capable("a"))

    def test_empty_name_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            SearchProviderRegistry([FakeProvider(name="")])


# ====================================================================== config
class TestSearchRouterConfig:
    ENV_VARS = (
        "SCRAPER_SEARCH_DEFAULT_PROVIDER",
        "SCRAPER_SEARCH_PROVIDER_ORDER",
        "SCRAPER_SEARCH_PROBE_TIMEOUT_SECONDS",
    )

    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in self.ENV_VARS:
            monkeypatch.delenv(var, raising=False)

    def test_defaults(self):
        config = SearchRouterConfig()
        assert config.default_provider == "auto"
        assert config.provider_order == []
        assert config.probe_timeout_seconds == 10.0

    def test_from_settings_without_search_block(self, monkeypatch: pytest.MonkeyPatch):
        self._clear_env(monkeypatch)
        config = SearchRouterConfig.from_settings(_settings_with_search_block(None))
        assert config.default_provider == "auto"
        assert config.provider_order == []

    def test_from_settings_yaml_block(self, monkeypatch: pytest.MonkeyPatch):
        self._clear_env(monkeypatch)
        settings = _settings_with_search_block(
            {
                "default_provider": "searxng",
                "provider_order": ["searxng", "camofox"],
                "probe_timeout_seconds": 5,
                "providers": {"searxng": {"enabled": False}},
            }
        )
        config = SearchRouterConfig.from_settings(settings)
        assert config.default_provider == "searxng"
        assert config.provider_order == ["searxng", "camofox"]
        assert config.probe_timeout_seconds == 5.0

    def test_from_settings_env_overrides_yaml(self, monkeypatch: pytest.MonkeyPatch):
        self._clear_env(monkeypatch)
        monkeypatch.setenv("SCRAPER_SEARCH_DEFAULT_PROVIDER", "b")
        monkeypatch.setenv("SCRAPER_SEARCH_PROVIDER_ORDER", "a, b ,c")
        monkeypatch.setenv("SCRAPER_SEARCH_PROBE_TIMEOUT_SECONDS", "2.5")
        settings = _settings_with_search_block(
            {"default_provider": "auto", "provider_order": ["z"]}
        )
        config = SearchRouterConfig.from_settings(settings)
        assert config.default_provider == "b"
        assert config.provider_order == ["a", "b", "c"]
        assert config.probe_timeout_seconds == 2.5

    def test_from_settings_ignores_unrelated_yaml_keys(self, monkeypatch: pytest.MonkeyPatch):
        self._clear_env(monkeypatch)
        settings = _settings_with_search_block({"providers": {"searxng": {"enabled": False}}})
        config = SearchRouterConfig.from_settings(settings)
        assert config.default_provider == "auto"

    def test_extra_field_rejected(self):
        with pytest.raises(Exception):
            SearchRouterConfig(bogus=1)  # type: ignore[call-arg]

    def test_invalid_probe_timeout_rejected(self):
        with pytest.raises(Exception):
            SearchRouterConfig(probe_timeout_seconds=0)


# ================================================================== explicit
class TestExplicitRouting:
    async def test_explicit_provider_selected(self):
        a, b = capable("a"), capable("b")
        router = make_router([a, b])

        response = await router.route(query(provider="a"))

        assert a.search_calls == 1
        assert b.search_calls == 0
        assert response.metadata.provider == "a"
        assert response.metadata.providers_attempted == ["a"]
        assert response.metadata.fallback_occurred is False
        assert response.results[0].provider == "a"

    async def test_unknown_provider_raises_validation_error(self):
        a, b = capable("a"), capable("b")
        router = make_router([a, b])

        with pytest.raises(ValidationError) as excinfo:
            await router.route(query(provider="ghost"))

        assert "ghost" in excinfo.value.message
        assert excinfo.value.details["available_providers"] == ["a", "b"]
        # No fallback: nothing is attempted.
        assert a.search_calls == 0 and b.search_calls == 0

    async def test_disabled_provider_treated_as_unknown(self):
        # A disabled provider is simply not registered by the factory;
        # requesting it explicitly is a deterministic validation error.
        router = make_router([capable("a")])
        with pytest.raises(ValidationError, match="not configured or is disabled"):
            await router.route(query(provider="b"))

    async def test_unavailable_provider_raises_unavailable(self):
        a = capable("a", available=False)
        router = make_router([a])

        with pytest.raises(SearchProviderUnavailableError, match="reports itself unavailable"):
            await router.route(query(provider="a"))
        assert a.search_calls == 0

    async def test_probe_failure_raises_unavailable(self):
        a = capable("a", capability_error=RuntimeError("boom"))
        router = make_router([a])

        with pytest.raises(SearchProviderUnavailableError, match="probe failed"):
            await router.route(query(provider="a"))
        assert a.search_calls == 0

    async def test_vertical_mismatch(self):
        a = capable("a", verticals=[Vertical.WEB])
        router = make_router([a])

        with pytest.raises(UnsupportedSearchCapabilityError) as excinfo:
            await router.route(query(provider="a", verticals=[Vertical.NEWS]))
        assert excinfo.value.details["missing_capabilities"] == ["vertical:news"]
        assert a.search_calls == 0

    async def test_time_range_mismatch(self):
        a = capable("a", time_ranges=[])
        router = make_router([a])

        with pytest.raises(UnsupportedSearchCapabilityError) as excinfo:
            await router.route(query(provider="a", time_range=TimeRange.DAY))
        assert excinfo.value.details["missing_capabilities"] == ["time_range"]

    async def test_language_mismatch(self):
        a = capable("a", supports_language=False)
        router = make_router([a])

        with pytest.raises(UnsupportedSearchCapabilityError) as excinfo:
            await router.route(query(provider="a", language="en"))
        assert excinfo.value.details["missing_capabilities"] == ["language"]

    async def test_safe_search_mismatch(self):
        a = capable("a", supports_safe_search=False)
        router = make_router([a])

        # The default request asks for MODERATE safe search: a provider
        # that cannot honor it is rejected (strict semantics).
        with pytest.raises(UnsupportedSearchCapabilityError) as excinfo:
            await router.route(query(provider="a"))
        assert excinfo.value.details["missing_capabilities"] == ["safe_search"]

    async def test_paging_mismatch(self):
        a = capable("a", supports_paging=False)
        router = make_router([a])

        with pytest.raises(UnsupportedSearchCapabilityError) as excinfo:
            await router.route(query(provider="a", page=2))
        assert excinfo.value.details["missing_capabilities"] == ["paging"]

    async def test_source_selection_mismatch(self):
        a = capable("a", supports_source_selection=False)
        router = make_router([a])

        with pytest.raises(UnsupportedSearchCapabilityError) as excinfo:
            await router.route(query(provider="a", sources=["bing"]))
        assert excinfo.value.details["missing_capabilities"] == ["sources"]

    async def test_degradable_limit_is_not_rejected(self):
        # ``limit`` is an upper bound, not a semantic filter: the provider
        # is still selected and the degradation is reported in metadata.
        a = capable("a", max_results=5)
        router = make_router([a])

        response = await router.route(query(provider="a", limit=50))

        assert a.search_calls == 1
        assert "limit" in response.metadata.degraded_capabilities
        assert response.metadata.fallback_occurred is False

    async def test_transient_failure_propagates_without_fallback(self):
        a = capable("a", search_error=SearchProviderTimeoutError("a timed out"))
        b = capable("b")
        router = make_router([a, b])

        with pytest.raises(SearchProviderTimeoutError):
            await router.route(query(provider="a"))
        assert a.search_calls == 1
        assert b.search_calls == 0  # explicit requests never fall back

    async def test_invalid_provider_response_propagates_without_fallback(self):
        a = capable("a", search_error=InvalidProviderResponseError("a returned garbage"))
        b = capable("b")
        router = make_router([a, b])

        # Even though this error class is fallback-eligible in auto mode,
        # an explicit request must surface it unchanged.
        with pytest.raises(InvalidProviderResponseError):
            await router.route(query(provider="a"))
        assert b.search_calls == 0

    async def test_unexpected_error_propagates(self):
        a = capable("a", search_error=RuntimeError("boom"))
        router = make_router([a])

        with pytest.raises(RuntimeError, match="boom"):
            await router.route(query(provider="a"))


# ====================================================================== auto
class TestAutoRouting:
    async def test_first_eligible_provider_selected(self):
        a, b = capable("a"), capable("b")
        router = make_router([a, b], provider_order=["a", "b"])

        response = await router.route(query())

        assert a.search_calls == 1
        assert b.search_calls == 0  # healthy but never needed
        assert b.capability_calls == 1  # probed, then bypassed
        assert response.metadata.provider == "a"
        assert response.metadata.providers_attempted == ["a"]
        assert response.metadata.fallback_occurred is False

    async def test_configured_provider_order_wins_over_registration(self):
        a, b = capable("a"), capable("b")
        # Registered a-first, but the config prefers b.
        router = make_router([a, b], provider_order=["b", "a"])

        response = await router.route(query())

        assert response.metadata.provider == "b"
        assert a.search_calls == 0

    async def test_registration_order_used_without_provider_order(self):
        a, b = capable("a"), capable("b")
        router = make_router([a, b])

        response = await router.route(query())
        assert response.metadata.provider == "a"

    async def test_unknown_order_name_is_skipped(self):
        a, b = capable("a"), capable("b")
        router = make_router([a, b], provider_order=["ghost", "b"])

        response = await router.route(query())
        assert response.metadata.provider == "b"

    async def test_unhealthy_provider_skipped(self):
        a, b = capable("a", available=False), capable("b")
        router = make_router([a, b], provider_order=["a", "b"])

        response = await router.route(query())

        assert response.metadata.provider == "b"
        assert response.metadata.providers_attempted == ["b"]
        assert response.metadata.fallback_occurred is False
        assert any("skipped: a (unavailable)" in w for w in response.metadata.warnings)

    async def test_probe_failure_skips_provider(self):
        a, b = capable("a", capability_error=RuntimeError("boom")), capable("b")
        router = make_router([a, b], provider_order=["a", "b"])

        response = await router.route(query())

        assert response.metadata.provider == "b"
        assert any("skipped: a" in w for w in response.metadata.warnings)

    async def test_probe_timeout_skips_provider(self):
        a = capable("a", capability_delay=1.0)
        b = capable("b")
        router = make_router([a, b], provider_order=["a", "b"], probe_timeout_seconds=0.05)

        response = await router.route(query())

        assert response.metadata.provider == "b"

    async def test_capability_mismatch_skipped_next_selected(self):
        # ``a`` cannot serve news; ``b`` can.
        a = capable("a", verticals=[Vertical.WEB])
        b = capable("b", verticals=[Vertical.WEB, Vertical.NEWS])
        router = make_router([a, b], provider_order=["a", "b"])

        response = await router.route(query(verticals=[Vertical.NEWS]))

        assert response.metadata.provider == "b"
        assert a.search_calls == 0
        assert any("skipped: a (capability mismatch)" in w for w in response.metadata.warnings)

    async def test_transient_unavailable_falls_back(self):
        a = capable("a", search_error=SearchProviderUnavailableError("a is down"))
        b = capable("b")
        router = make_router([a, b], provider_order=["a", "b"])

        response = await router.route(query())

        assert response.metadata.provider == "b"
        assert response.metadata.providers_attempted == ["a", "b"]
        assert response.metadata.fallback_occurred is True
        assert any("falling back to 'b'" in w for w in response.metadata.warnings)

    async def test_transient_timeout_falls_back(self):
        a = capable("a", search_error=SearchProviderTimeoutError("a timed out"))
        b = capable("b")
        router = make_router([a, b], provider_order=["a", "b"])

        response = await router.route(query())
        assert response.metadata.fallback_occurred is True
        assert response.metadata.providers_attempted == ["a", "b"]

    async def test_invalid_response_falls_back(self):
        # A provider that answers with an unparseable payload is an
        # upstream-side failure: auto mode falls back to the next provider.
        a = capable("a", search_error=InvalidProviderResponseError("garbage payload"))
        b = capable("b")
        router = make_router([a, b], provider_order=["a", "b"])

        response = await router.route(query())
        assert response.metadata.provider == "b"
        assert response.metadata.fallback_occurred is True

    async def test_fallback_chain_across_multiple_providers(self):
        a = capable("a", search_error=SearchProviderUnavailableError("a down"))
        b = capable("b", search_error=SearchProviderTimeoutError("b slow"))
        c = capable("c")
        router = make_router([a, b, c], provider_order=["a", "b", "c"])

        response = await router.route(query())

        assert response.metadata.provider == "c"
        assert response.metadata.providers_attempted == ["a", "b", "c"]
        assert response.metadata.fallback_occurred is True
        fallback_warnings = [w for w in response.metadata.warnings if "falling back" in w]
        assert len(fallback_warnings) == 2

    async def test_all_eligible_fail_raises_no_provider_available(self):
        a = capable("a", search_error=SearchProviderUnavailableError("a down"))
        b = capable("b", search_error=SearchProviderTimeoutError("b slow"))
        router = make_router([a, b], provider_order=["a", "b"])

        with pytest.raises(NoProviderAvailableError) as excinfo:
            await router.route(query())

        details = excinfo.value.details
        assert details["providers_attempted"] == ["a", "b"]
        assert [f["error"] for f in details["failures"]] == [
            "search_provider_unavailable",
            "search_provider_timeout",
        ]

    async def test_empty_registry_raises_no_provider_available(self):
        router = make_router([])
        with pytest.raises(NoProviderAvailableError, match="No search providers are configured"):
            await router.route(query())

    async def test_no_eligible_provider_raises_no_provider_available(self):
        a = capable("a", available=False)
        b = capable("b", verticals=[Vertical.WEB])  # cannot serve news
        c = capable("c", capability_error=RuntimeError("boom"))
        router = make_router([a, b, c], provider_order=["a", "b", "c"])

        with pytest.raises(NoProviderAvailableError) as excinfo:
            await router.route(query(verticals=[Vertical.NEWS]))

        candidates = excinfo.value.details["candidates"]
        assert [entry["provider"] for entry in candidates] == ["a", "b", "c"]
        assert [entry["reason"] for entry in candidates] == [
            "unavailable",
            "capability mismatch",
            "unavailable (probe failed)",
        ]
        assert a.search_calls == 0 and b.search_calls == 0 and c.search_calls == 0

    async def test_no_fallback_on_invalid_request(self):
        # An invalid (unknown) explicit provider is a caller error: the
        # router must reject it deterministically, never try other providers.
        a, b = capable("a"), capable("b")
        router = make_router([a, b], provider_order=["a", "b"])

        with pytest.raises(ValidationError):
            await router.route(query(provider="ghost"))
        assert a.search_calls == 0 and b.search_calls == 0

    async def test_metadata_records_degraded_limit(self):
        a = capable("a", max_results=5)
        router = make_router([a])

        response = await router.route(query(limit=50))
        assert "limit" in response.metadata.degraded_capabilities

    async def test_response_elapsed_ms_reported(self):
        a = capable("a")
        router = make_router([a])

        response = await router.route(query())
        assert isinstance(response.metadata.elapsed_ms, int)
        assert response.metadata.elapsed_ms >= 0


# ================================================================== default
class TestDefaultProviderConfig:
    async def test_default_auto_is_the_default(self):
        a, b = capable("a"), capable("b")
        router = make_router([a, b])
        assert router.config.default_provider == "auto"

    async def test_default_provider_name_routes_auto_requests_explicitly(self):
        a = capable("a", search_error=SearchProviderUnavailableError("a down"))
        b = capable("b")
        router = make_router([a, b], default_provider="a")

        # provider=auto now means "explicit a": no fallback to b.
        with pytest.raises(SearchProviderUnavailableError):
            await router.route(query())
        assert a.search_calls == 1
        assert b.search_calls == 0

    async def test_default_provider_capability_mismatch_is_rejected(self):
        a = capable("a", verticals=[Vertical.WEB])
        b = capable("b", verticals=[Vertical.WEB, Vertical.NEWS])
        router = make_router([a, b], default_provider="a")

        with pytest.raises(UnsupportedSearchCapabilityError):
            await router.route(query(verticals=[Vertical.NEWS]))
        assert b.search_calls == 0

    def test_default_provider_must_be_registered(self):
        with pytest.raises(ValueError, match="no such provider is configured"):
            SearchRouter(
                SearchProviderRegistry([capable("a")]), SearchRouterConfig(default_provider="ghost")
            )

    async def test_request_beats_default(self):
        a, b = capable("a"), capable("b")
        router = make_router([a, b], default_provider="a")

        response = await router.route(query(provider="b"))
        assert response.metadata.provider == "b"
        assert a.search_calls == 0


# =================================================================== build
class TestBuildSearchRouter:
    async def test_builds_configured_searxng_provider(self):
        settings = _settings_with_search_block(
            {
                "default_provider": "auto",
                "provider_order": ["searxng"],
                "probe_timeout_seconds": 5,
                "providers": {
                    "searxng": {
                        "enabled": True,
                        "base_url": "http://127.0.0.1:1",  # refuses connections fast
                    }
                },
            }
        )

        router = build_search_router(settings)
        try:
            assert router.registry.names == ["searxng"]
            assert router.config.default_provider == "auto"
            assert router.config.provider_order == ["searxng"]
            assert router.config.probe_timeout_seconds == 5.0
            # Routing through the real (offline) SearXNG provider: the probe
            # reports it unavailable, so auto routing finds no eligible
            # provider — proving the router is backend-agnostic.
            with pytest.raises(NoProviderAvailableError):
                await router.route(query())
        finally:
            for provider in router.registry.all():
                close = getattr(provider, "close", None)
                if close is not None:
                    await close()

    async def test_no_search_block_yields_empty_registry(self):
        router = build_search_router(_settings_with_search_block(None))
        assert router.registry.names == []
        with pytest.raises(NoProviderAvailableError, match="No search providers are configured"):
            await router.route(query())

    async def test_disabled_provider_not_registered(self):
        settings = _settings_with_search_block({"providers": {"searxng": {"enabled": False}}})
        router = build_search_router(settings)
        assert router.registry.names == []

    def test_invalid_default_provider_raises(self):
        settings = _settings_with_search_block(
            {
                "default_provider": "camofox",  # not implemented / not registered
                "providers": {"searxng": {"enabled": True, "base_url": "http://127.0.0.1:1"}},
            }
        )
        with pytest.raises(ValueError, match="no such provider is configured"):
            build_search_router(settings)


# =================================================================== redact
class TestRedactSecrets:
    def test_url_userinfo_removed(self):
        assert (
            redact_secrets("http://admin:secret@searxng:8080/config unreachable")
            == "http://searxng:8080/config unreachable"
        )

    def test_key_value_credentials_redacted(self):
        assert redact_secrets("password=hunter2") == "password=[REDACTED]"
        assert redact_secrets("token: abc123") == "token: [REDACTED]"
        assert redact_secrets("api_key = k-999") == "api_key = [REDACTED]"
        assert redact_secrets("API Key = k-999") == "API Key = [REDACTED]"
        assert redact_secrets("secret=topsecret") == "secret=[REDACTED]"

    def test_authorization_header_redacted(self):
        assert redact_secrets("Authorization: Bearer sk-live-123456") == "authorization [REDACTED]"

    def test_bearer_token_redacted(self):
        assert redact_secrets("got Bearer sk-live-123456 back") == "got bearer [REDACTED] back"

    def test_plain_message_unchanged(self):
        text = "SearXNG /search returned HTTP 502"
        assert redact_secrets(text) == text

    async def test_fallback_warnings_never_leak_secrets(self):
        a = capable(
            "a",
            search_error=SearchProviderUnavailableError(
                "unreachable: http://user:pass@upstream/api token=abc123"
            ),
        )
        b = capable("b")
        router = make_router([a, b], provider_order=["a", "b"])

        response = await router.route(query())
        joined = " ".join(response.metadata.warnings)
        assert "pass@" not in joined
        assert "abc123" not in joined
        assert "[REDACTED]" in joined


# =============================================================== integration
class TestProtocolCompliance:
    def test_fake_provider_satisfies_protocol(self):
        assert isinstance(capable("fake"), SearchProvider)

    def test_registry_holds_protocol_objects(self):
        registry = SearchProviderRegistry([capable("a")])
        provider = registry.get("a")
        assert isinstance(provider, SearchProvider)
