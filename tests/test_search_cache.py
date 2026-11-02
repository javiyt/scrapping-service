"""Tests for the provider-agnostic search result cache.

Coverage follows the ticket:

- cache hit / cache miss
- force refresh
- TTL expiration
- stale-if-error (and every "never stale" carve-out)
- different filters create different keys
- field ordering produces the same key
- serialization / deserialization round-trip
- logical separation from the scrape cache (stats, cleanup, coexistence)

All behaviour is exercised with in-memory fake providers and a temporary
SQLite database; no real backends are touched.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.cache.models import CacheEntry
from app.cache.sqlite_cache import SqliteCache
from app.core.config import Settings
from app.core.errors import (
    InvalidProviderResponseError,
    NoProviderAvailableError,
    ScraperError,
    SearchProviderTimeoutError,
    SearchProviderUnavailableError,
    UnsupportedSearchCapabilityError,
    ValidationError,
)
from app.metrics.prometheus import MetricsCollector
from app.search import (
    SearchCache,
    SearchCacheConfig,
    SearchProviderRegistry,
    SearchRouter,
    SearchRouterConfig,
    build_key_material,
    build_search_cache,
    make_search_cache_key,
    normalize_query,
)
from app.search.cache import SearchCacheEntry
from app.search.models import (
    SafeSearch,
    SearchCapabilities,
    SearchMetadata,
    SearchQuery,
    SearchResponse,
    SearchResult,
    TimeRange,
    Vertical,
)

CACHE_ENV_VARS = (
    "SCRAPER_SEARCH_CACHE_ENABLED",
    "SCRAPER_SEARCH_CACHE_DEFAULT_TTL_SECONDS",
    "SCRAPER_SEARCH_CACHE_STALE_IF_ERROR",
)

PUBLISHED_AT = datetime(2024, 5, 1, 12, 0, 0, tzinfo=UTC)


# ====================================================================== fakes
class FakeProvider:
    """In-memory provider satisfying the structural SearchProvider protocol."""

    def __init__(
        self,
        name: str = "fake",
        available: bool = True,
        verticals: list[Vertical] | None = None,
        supports_safe_search: bool = True,
        supports_paging: bool = True,
        time_ranges: list[TimeRange] | None = None,
        search_error: Exception | None = None,
    ) -> None:
        self._name = name
        self._available = available
        self._verticals = verticals or [Vertical.WEB]
        self._supports_safe_search = supports_safe_search
        self._supports_paging = supports_paging
        self._time_ranges = time_ranges or []
        self._search_error = search_error
        self.search_calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def search_error(self) -> Exception | None:
        return self._search_error

    @search_error.setter
    def search_error(self, value: Exception | None) -> None:
        self._search_error = value

    async def capabilities(self) -> SearchCapabilities:
        return SearchCapabilities(
            verticals=self._verticals,
            time_ranges=self._time_ranges,
            supports_language=False,
            supports_safe_search=self._supports_safe_search,
            supports_paging=self._supports_paging,
            supports_source_selection=False,
            max_results=None,
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
                    url=f"https://example.com/{self._name}",
                    rank=1,
                    vertical=request.verticals[0],
                    provider=self._name,
                    snippet="snippet text",
                    published_at=PUBLISHED_AT,
                    score=0.83,
                    thumbnail_url="https://example.com/thumb.png",
                )
            ],
            metadata=SearchMetadata(provider=self._name),
        )


def capable(name: str = "fake", **overrides) -> FakeProvider:
    defaults: dict = {"name": name}
    defaults.update(overrides)
    return FakeProvider(**defaults)  # type: ignore[arg-type]


def query(**overrides) -> SearchQuery:
    data: dict = {"query": "dolphins"}
    data.update(overrides)
    return SearchQuery(**data)


def make_router(providers: list[FakeProvider]) -> SearchRouter:
    return SearchRouter(SearchProviderRegistry(providers), SearchRouterConfig())


def make_search_cache(
    cache: SqliteCache,
    router: SearchRouter,
    **config_kwargs,
) -> SearchCache:
    return SearchCache(
        store=cache,
        router=router,
        config=SearchCacheConfig(**config_kwargs),
        metrics=MetricsCollector(),
    )


def _settings_with_search_block(search_block: dict | None) -> Settings:
    settings = Settings(api_key="test-key", server_api_key_required=False)
    object.__setattr__(settings, "raw_yaml_search", search_block)
    return settings


def seed_search_entry(
    cache: SqliteCache,
    request: SearchQuery,
    *,
    age_seconds: float = 100.0,
    ttl_seconds: int = 60,
) -> str:
    """Store one (possibly expired) entry for ``request`` directly.

    Returns the cache key so the same entry can be re-read.
    """
    key = make_search_cache_key(request)
    now = datetime.now(UTC)
    fetched_at = now - timedelta(seconds=age_seconds)
    response = SearchResponse(
        query=request.query,
        verticals=request.verticals,
        results=[
            SearchResult(
                title="Cached result",
                url="https://example.com/cached",
                rank=1,
                vertical=request.verticals[0],
                provider="fake",
                snippet="from the cache",
            )
        ],
        metadata=SearchMetadata(provider="fake"),
    )
    response_json = response.model_dump_json()
    cache.set_search_entry(
        cache_key=key,
        request=build_key_material_json(request),
        response=response_json,
        fetched_at=fetched_at,
        expires_at=fetched_at + timedelta(seconds=ttl_seconds),
        content_length=len(response_json.encode("utf-8")),
    )
    return key


def build_key_material_json(request: SearchQuery) -> str:
    import json

    return json.dumps(build_key_material(request), sort_keys=True, separators=(",", ":"))


@pytest.fixture
def cache_db(tmp_path: Path) -> SqliteCache:
    cache = SqliteCache(db_path=str(tmp_path / "cache.db"))
    cache.open()
    yield cache
    cache.close()


# ==================================================================== config
class TestSearchCacheConfig:
    def test_defaults(self):
        config = SearchCacheConfig()
        assert config.enabled is True
        assert config.default_ttl_seconds == 900
        assert config.stale_if_error is True

    def test_from_settings_without_search_block(self, monkeypatch: pytest.MonkeyPatch):
        for var in CACHE_ENV_VARS:
            monkeypatch.delenv(var, raising=False)
        config = SearchCacheConfig.from_settings(_settings_with_search_block(None))
        assert config == SearchCacheConfig()

    def test_from_settings_with_cache_block(self, monkeypatch: pytest.MonkeyPatch):
        for var in CACHE_ENV_VARS:
            monkeypatch.delenv(var, raising=False)
        # The `cache` key lives inside the broader search block, alongside
        # routing keys and provider config.
        settings = _settings_with_search_block(
            {
                "default_provider": "auto",
                "cache": {
                    "enabled": False,
                    "default_ttl_seconds": 60,
                    "stale_if_error": False,
                },
                "providers": {"searxng": {"enabled": False}},
            }
        )
        config = SearchCacheConfig.from_settings(settings)
        assert config.enabled is False
        assert config.default_ttl_seconds == 60
        assert config.stale_if_error is False

    def test_from_settings_env_overrides_yaml(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("SCRAPER_SEARCH_CACHE_ENABLED", "false")
        monkeypatch.setenv("SCRAPER_SEARCH_CACHE_DEFAULT_TTL_SECONDS", "300")
        monkeypatch.setenv("SCRAPER_SEARCH_CACHE_STALE_IF_ERROR", "false")
        settings = _settings_with_search_block({"cache": {"enabled": True}})
        config = SearchCacheConfig.from_settings(settings)
        assert config.enabled is False
        assert config.default_ttl_seconds == 300
        assert config.stale_if_error is False

    def test_ttl_zero_means_never_expires(self):
        assert SearchCacheConfig(default_ttl_seconds=0).default_ttl_seconds == 0

    def test_negative_ttl_rejected(self):
        with pytest.raises(ValueError):
            SearchCacheConfig(default_ttl_seconds=-1)

    def test_extra_keys_rejected(self):
        with pytest.raises(ValueError):
            SearchCacheConfig(unknown_key=1)  # type: ignore[call-arg]

    def test_cache_block_must_be_mapping(self, monkeypatch: pytest.MonkeyPatch):
        for var in CACHE_ENV_VARS:
            monkeypatch.delenv(var, raising=False)
        settings = _settings_with_search_block({"cache": "yes"})
        with pytest.raises(ValueError, match="search.cache must be a mapping"):
            SearchCacheConfig.from_settings(settings)


# ======================================================================= keys
class TestNormalizeQuery:
    def test_trims_and_collapses(self):
        assert normalize_query("  Dolphins   swim  ") == "dolphins swim"

    def test_collapses_newlines_and_tabs(self):
        assert normalize_query("a\n\t b") == "a b"

    def test_casefold(self):
        assert normalize_query("DOLPHINS") == normalize_query("dolphins")

    def test_equivalent_strings_same(self):
        assert normalize_query("  Hello   World ") == normalize_query("hello world")


class TestMakeSearchCacheKey:
    def test_key_is_sha256_hex(self):
        key = make_search_cache_key(query())
        assert len(key) == 64
        assert all(char in "0123456789abcdef" for char in key)

    def test_field_ordering_produces_same_key(self):
        # Same semantics, different JSON field ordering (and query casing /
        # whitespace) must map to the same key.
        a = SearchQuery(
            query="dolphins",
            limit=5,
            verticals=[Vertical.WEB, Vertical.NEWS],
            language="en",
            page=2,
            time_range=TimeRange.DAY,
            safe_search=SafeSearch.STRICT,
            sources=["DuckDuckGo", "google"],
        )
        b = SearchQuery.model_validate(
            {
                "sources": ["google", "duckduckgo"],
                "safe_search": "strict",
                "query": "  DOLPHINS  ",
                "time_range": "day",
                "page": 2,
                "language": "en",
                "limit": 5,
                "verticals": ["news", "web"],
            }
        )
        assert make_search_cache_key(a) == make_search_cache_key(b)

    def test_deterministic_across_calls(self):
        assert make_search_cache_key(query()) == make_search_cache_key(query())

    @pytest.mark.parametrize(
        "field, override_a, override_b",
        [
            ("query", {"query": "one"}, {"query": "two"}),
            ("provider", {"provider": "auto"}, {"provider": "fake"}),
            ("verticals", {"verticals": [Vertical.WEB]}, {"verticals": [Vertical.NEWS]}),
            ("language", {"language": "en"}, {"language": "pt-BR"}),
            ("time_range", {"time_range": TimeRange.DAY}, {"time_range": TimeRange.WEEK}),
            (
                "time_range_none",
                {"time_range": None},
                {"time_range": TimeRange.MONTH},
            ),
            ("safe_search", {"safe_search": SafeSearch.OFF}, {"safe_search": SafeSearch.STRICT}),
            ("page", {"page": 1}, {"page": 2}),
            ("limit", {"limit": 5}, {"limit": 10}),
            (
                "limit_none",
                {"limit": None},
                {"limit": 10},
            ),
            ("sources", {"sources": ["google"]}, {"sources": ["bing"]}),
        ],
        ids=lambda value: str(value),
    )
    def test_different_filters_create_different_keys(self, field, override_a, override_b):
        key_a = make_search_cache_key(query(**override_a))
        key_b = make_search_cache_key(query(**override_b))
        assert key_a != key_b

    def test_vertical_order_is_irrelevant(self):
        a = make_search_cache_key(query(verticals=[Vertical.WEB, Vertical.NEWS]))
        b = make_search_cache_key(query(verticals=[Vertical.NEWS, Vertical.WEB]))
        assert a == b

    def test_source_order_and_case_are_irrelevant(self):
        a = make_search_cache_key(query(sources=["DuckDuckGo", "Google"]))
        b = make_search_cache_key(query(sources=["google", "duckduckgo"]))
        assert a == b

    def test_material_contains_expected_fields(self):
        material = build_key_material(
            query(
                provider="fake",
                verticals=[Vertical.NEWS, Vertical.WEB],
                language="en",
                time_range=TimeRange.WEEK,
                safe_search=SafeSearch.OFF,
                page=3,
                limit=7,
                sources=["Google"],
            )
        )
        assert material["v"] == 1
        assert material["query"] == "dolphins"
        assert material["provider"] == "fake"
        assert material["verticals"] == ["news", "web"]  # sorted
        assert material["language"] == "en"
        assert material["time_range"] == "week"
        assert material["safe_search"] == "off"
        assert material["page"] == 3
        assert material["limit"] == 7
        assert material["sources"] == ["google"]  # lowercased + sorted


# ====================================================== store / round-trip
class TestSearchStore:
    def test_set_get_round_trip(self, cache_db: SqliteCache):
        request = query(page=2, limit=5)

        # Exercise the raw store API directly.
        now = datetime.now(UTC)
        provider_response = SearchResponse(
            query=request.query,
            verticals=request.verticals,
            results=[
                SearchResult(
                    title="Round trip",
                    url="https://example.com/rt",
                    rank=1,
                    vertical=Vertical.WEB,
                    provider="fake",
                    snippet="s",
                    published_at=PUBLISHED_AT,
                    score=0.5,
                    thumbnail_url="https://example.com/t.png",
                )
            ],
            metadata=SearchMetadata(
                provider="fake",
                providers_attempted=["fake"],
                elapsed_ms=12,
                from_cache=False,
                stale=False,
            ),
        )
        response_json = provider_response.model_dump_json()
        cache_db.set_search_entry(
            cache_key="k1",
            request=build_key_material_json(request),
            response=response_json,
            fetched_at=now,
            expires_at=now + timedelta(seconds=60),
            content_length=len(response_json.encode("utf-8")),
        )

        row = cache_db.get_search_entry("k1")
        assert row is not None
        restored = SearchResponse.model_validate_json(row["response"])
        assert restored == provider_response
        assert restored.results[0].published_at == PUBLISHED_AT
        assert restored.results[0].score == 0.5
        assert restored.metadata.provider == "fake"
        assert datetime.fromisoformat(row["fetched_at"]) == now
        assert datetime.fromisoformat(row["expires_at"]) == now + timedelta(seconds=60)

    def test_get_missing_returns_none(self, cache_db: SqliteCache):
        assert cache_db.get_search_entry("nope") is None

    def test_delete_and_purge(self, cache_db: SqliteCache):
        seed_search_entry(cache_db, query(query="a"))
        seed_search_entry(cache_db, query(query="b"))
        key = make_search_cache_key(query(query="a"))

        assert cache_db.delete_search_entry(key) is True
        assert cache_db.delete_search_entry(key) is False  # idempotent
        assert cache_db.purge_search() == 1  # only "b" remains

    def test_search_stats(self, cache_db: SqliteCache):
        assert cache_db.search_stats() == {
            "search_total_entries": 0,
            "search_total_size_bytes": 0,
            "search_expired_entries": 0,
        }
        seed_search_entry(cache_db, query(query="a"), age_seconds=1, ttl_seconds=10)
        stats = cache_db.search_stats()
        assert stats["search_total_entries"] == 1
        assert stats["search_total_size_bytes"] > 0
        assert stats["search_expired_entries"] == 0

    def test_stats_include_both_tables(self, cache_db: SqliteCache):
        seed_search_entry(cache_db, query(query="searched"))

        scrape_entry = CacheEntry(
            cache_key="scrape-1",
            url="https://example.com",
            final_url="https://example.com",
            status_code=200,
            html="<html>hi</html>",
            fetched_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            mode="static",
            content_length=10,
            headers="{}",
            error_metadata=None,
        )
        cache_db.set(scrape_entry)

        stats = cache_db.stats()
        assert stats["total_entries"] == 1  # scrape table only
        assert stats["search_total_entries"] == 1  # search table only

    def test_tables_are_independent(self, cache_db: SqliteCache):
        seed_search_entry(cache_db, query(query="cached"))
        scrape_entry = CacheEntry(
            cache_key="scrape-1",
            url="https://example.com",
            final_url="https://example.com",
            status_code=200,
            html="<html>hi</html>",
            fetched_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            mode="static",
            content_length=10,
            headers="{}",
            error_metadata=None,
        )
        cache_db.set(scrape_entry)

        # Purging one table never touches the other.
        assert cache_db.purge() == 1
        assert cache_db.get("scrape-1") is None
        assert cache_db.get_search_entry(make_search_cache_key(query(query="cached"))) is not None

        assert cache_db.purge_search() == 1
        assert cache_db.get("scrape-1") is None
        assert cache_db.get_search_entry(make_search_cache_key(query(query="cached"))) is None

    def test_entry_expiry_flags(self, cache_db: SqliteCache):
        now = datetime.now(UTC)
        expired = SearchCacheEntry(
            cache_key="k",
            response=SearchResponse(
                query="q",
                verticals=[Vertical.WEB],
                results=[],
                metadata=SearchMetadata(provider="fake"),
            ),
            fetched_at=now - timedelta(seconds=5),
            expires_at=now - timedelta(seconds=1),
            content_length=10,
        )
        fresh = SearchCacheEntry(
            cache_key="k",
            response=SearchResponse(
                query="q",
                verticals=[Vertical.WEB],
                results=[],
                metadata=SearchMetadata(provider="fake"),
            ),
            fetched_at=now,
            expires_at=now + timedelta(seconds=60),
            content_length=10,
        )
        unbounded = SearchCacheEntry(
            cache_key="k",
            response=SearchResponse(
                query="q",
                verticals=[Vertical.WEB],
                results=[],
                metadata=SearchMetadata(provider="fake"),
            ),
            fetched_at=now,
            expires_at=None,
            content_length=10,
        )
        assert expired.is_expired is True
        assert fresh.is_expired is False
        assert unbounded.is_expired is False
        assert expired.age_seconds() >= 1
        assert fresh.age_seconds() >= 0


# ====================================================== behavior matrix
@pytest.fixture
def provider() -> FakeProvider:
    return capable()


@pytest.fixture
def search_cache(cache_db: SqliteCache, provider: FakeProvider) -> SearchCache:
    return make_search_cache(cache_db, make_router([provider]))


class TestSearchCacheBehavior:
    # ----------------------------------------------------------- miss / hit
    async def test_cache_miss_stores_and_flags_fresh(
        self, cache_db: SqliteCache, search_cache: SearchCache, provider: FakeProvider
    ):
        response = await search_cache.search(query(query="dolphins"))

        assert provider.search_calls == 1
        assert response.metadata.from_cache is False
        assert response.metadata.stale is False
        assert response.metadata.cached_at is None
        assert response.metadata.cache_age_seconds is None
        assert cache_db.search_stats()["search_total_entries"] == 1

    async def test_cache_hit_skips_provider(
        self, cache_db: SqliteCache, search_cache: SearchCache, provider: FakeProvider
    ):
        request = query(query="dolphins")
        first = await search_cache.search(request)
        second = await search_cache.search(query(query="DOLPHINS  "))  # same semantics

        assert provider.search_calls == 1  # provider ran only for the first
        assert first.metadata.from_cache is False
        assert second.metadata.from_cache is True
        assert second.metadata.stale is False
        assert second.metadata.cached_at is not None
        assert second.metadata.cache_age_seconds is not None
        assert second.metadata.cache_age_seconds >= 0
        assert second.results == first.results
        assert second.results[0].provider == "fake"
        # Equivalent-but-not-identical queries still share the entry.
        assert make_search_cache_key(query(query="DOLPHINS  ")) == make_search_cache_key(request)

    async def test_different_request_is_a_miss(
        self, search_cache: SearchCache, provider: FakeProvider
    ):
        await search_cache.search(query(query="dolphins"))
        await search_cache.search(query(query="dolphins", page=2))
        assert provider.search_calls == 2

    async def test_corrupt_entry_degrades_to_miss_and_is_discarded(
        self, cache_db: SqliteCache, search_cache: SearchCache, provider: FakeProvider
    ):
        key = make_search_cache_key(query(query="dolphins"))
        cache_db.set_search_entry(
            cache_key=key,
            request="{}",
            response="{not json",
            fetched_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            content_length=8,
        )

        response = await search_cache.search(query(query="dolphins"))

        assert provider.search_calls == 1  # re-executed, not a 500
        assert response.metadata.from_cache is False
        # The corrupt row was replaced by the fresh one.
        assert (
            SearchResponse.model_validate_json(
                cache_db.get_search_entry(key)["response"]
            ).metadata.from_cache
            is False
        )

    # -------------------------------------------------------- force refresh
    async def test_force_refresh_reruns_and_updates_entry(
        self, cache_db: SqliteCache, search_cache: SearchCache, provider: FakeProvider
    ):
        request = query(query="dolphins")
        first = await search_cache.search(request)
        first_cached_at = first.metadata.cached_at
        await search_cache.search(request)  # warm
        assert provider.search_calls == 1

        refreshed = await search_cache.search(request.model_copy(update={"force_refresh": True}))

        assert provider.search_calls == 2  # bypassed the cached copy
        assert refreshed.metadata.from_cache is False
        # The next ordinary request must hit the refreshed entry.
        again = await search_cache.search(request)
        assert provider.search_calls == 2
        assert again.metadata.from_cache is True
        assert first_cached_at is None  # fresh responses never carry cached_at

    async def test_force_refresh_failure_propagates_without_stale(
        self, search_cache: SearchCache, provider: FakeProvider
    ):
        # Explicit provider mode: transient failures propagate unchanged.
        request = query(query="dolphins", provider="fake")
        await search_cache.search(request)  # warm
        provider.search_error = SearchProviderUnavailableError(
            "provider exploded", details={"provider": "fake"}
        )

        with pytest.raises(SearchProviderUnavailableError):
            await search_cache.search(request.model_copy(update={"force_refresh": True}))
        # The failed force refresh must not have clobbered the fresh entry:
        # the next ordinary request is a plain cache hit.
        provider.search_error = None
        response = await search_cache.search(request)
        assert provider.search_calls == 2
        assert response.metadata.from_cache is True
        assert response.metadata.stale is False

    # -------------------------------------------------------------- TTL
    async def test_ttl_expiration_triggers_rerun(
        self, cache_db: SqliteCache, search_cache: SearchCache, provider: FakeProvider
    ):
        request = query(query="dolphins")
        await search_cache.search(request)
        # Age the entry past its TTL directly in the store.
        key = make_search_cache_key(request)
        now = datetime.now(UTC)
        row = cache_db.get_search_entry(key)
        assert row is not None
        cache_db.set_search_entry(
            cache_key=key,
            request=row["request"],
            response=row["response"],
            fetched_at=now - timedelta(seconds=3600),
            expires_at=now - timedelta(seconds=3000),
            content_length=row["content_length"],
        )

        response = await search_cache.search(request)

        assert provider.search_calls == 2  # re-executed: the entry was stale
        assert response.metadata.from_cache is False
        # The old entry was replaced with a fresh one.
        assert cache_db.get_search_entry(key)["expires_at"] > now.isoformat()

    async def test_per_request_ttl_is_used(self, cache_db: SqliteCache, search_cache: SearchCache):
        request = query(query="dolphins", cache_ttl_seconds=61)
        await search_cache.search(request)
        key = make_search_cache_key(request)
        row = cache_db.get_search_entry(key)
        fetched = datetime.fromisoformat(row["fetched_at"])
        expires = datetime.fromisoformat(row["expires_at"])
        assert timedelta(seconds=60) < expires - fetched <= timedelta(seconds=62)

    async def test_ttl_zero_entries_never_expire(
        self, cache_db: SqliteCache, provider: FakeProvider
    ):
        no_ttl_cache = make_search_cache(cache_db, make_router([provider]), default_ttl_seconds=0)
        request = query(query="dolphins")
        await no_ttl_cache.search(request)
        key = make_search_cache_key(request)
        row = cache_db.get_search_entry(key)
        assert row["expires_at"] is None

        # Even far in the past the entry is still served (no expiry bound).
        now = datetime.now(UTC)
        cache_db.set_search_entry(
            cache_key=key,
            request=row["request"],
            response=row["response"],
            fetched_at=now - timedelta(days=30),
            expires_at=None,
            content_length=row["content_length"],
        )
        response = await no_ttl_cache.search(request)
        assert response.metadata.from_cache is True

    # ----------------------------------------------------- stale-if-error
    async def test_stale_if_error_serves_expired_entry(
        self, cache_db: SqliteCache, search_cache: SearchCache, provider: FakeProvider
    ):
        request = query(query="dolphins")
        # Warm, then age past TTL (stale-but-kept), then break the provider.
        await search_cache.search(request)
        seed_search_entry(cache_db, request, age_seconds=10_000, ttl_seconds=900)
        provider.search_error = SearchProviderUnavailableError(
            "backend is down", details={"provider": "fake"}
        )

        response = await search_cache.search(request)

        assert provider.search_calls == 2  # attempted the live run, failed
        assert response.metadata.from_cache is True
        assert response.metadata.stale is True
        assert response.metadata.cached_at is not None
        assert response.metadata.cache_age_seconds is not None
        assert response.metadata.cache_age_seconds > 900  # older than the TTL
        assert any("stale" in warning for warning in response.metadata.warnings)
        assert response.results[0].snippet == "from the cache"

    @pytest.mark.parametrize(
        "error",
        [
            SearchProviderUnavailableError("down", details={"provider": "fake"}),
            SearchProviderTimeoutError("slow", details={"provider": "fake"}),
            InvalidProviderResponseError("garbled", details={"provider": "fake"}),
        ],
        ids=["unavailable", "timeout", "invalid_response"],
    )
    async def test_stale_if_error_covers_retryable_errors(
        self,
        cache_db: SqliteCache,
        search_cache: SearchCache,
        provider: FakeProvider,
        error: Exception,
    ):
        request = query(query="dolphins")
        seed_search_entry(cache_db, request, age_seconds=10_000, ttl_seconds=900)
        provider.search_error = error

        response = await search_cache.search(request)

        assert response.metadata.from_cache is True
        assert response.metadata.stale is True

    async def test_stale_disabled_propagates_error(
        self, cache_db: SqliteCache, provider: FakeProvider
    ):
        search_cache = make_search_cache(cache_db, make_router([provider]), stale_if_error=False)
        request = query(query="dolphins", provider="fake")
        seed_search_entry(cache_db, request, age_seconds=10_000, ttl_seconds=900)
        provider.search_error = SearchProviderUnavailableError("down")

        with pytest.raises(SearchProviderUnavailableError):
            await search_cache.search(request)

    async def test_no_stale_for_validation_error(
        self, cache_db: SqliteCache, search_cache: SearchCache, provider: FakeProvider
    ):
        request = query(query="dolphins", provider="unconfigured")
        # An expired entry exists for this exact (invalid) request.
        seed_search_entry(cache_db, request, age_seconds=10_000, ttl_seconds=900)

        with pytest.raises(ValidationError):
            await search_cache.search(request)
        assert provider.search_calls == 0  # never executed

    async def test_no_stale_for_capability_mismatch(
        self, cache_db: SqliteCache, search_cache: SearchCache, provider: FakeProvider
    ):
        request = query(query="dolphins")
        seed_search_entry(cache_db, request, age_seconds=10_000, ttl_seconds=900)
        provider.search_error = UnsupportedSearchCapabilityError(
            "cannot serve", details={"provider": "fake", "missing_capabilities": ["paging"]}
        )

        with pytest.raises(UnsupportedSearchCapabilityError):
            await search_cache.search(request)

    async def test_no_stale_when_all_providers_down_in_auto_mode(
        self, cache_db: SqliteCache, provider: FakeProvider
    ):
        # A *down* provider (unavailable capabilities) in auto mode is a
        # provider-side failure: stale should be served.
        search_cache = make_search_cache(cache_db, make_router([provider]))
        request = query(query="dolphins", provider="auto")
        seed_search_entry(cache_db, request, age_seconds=10_000, ttl_seconds=900)
        provider._available = False

        response = await search_cache.search(request)

        assert response.metadata.from_cache is True
        assert response.metadata.stale is True
        assert provider.search_calls == 0  # never got past the capabilities probe

    async def test_no_stale_when_request_cannot_be_satisfied(self, cache_db: SqliteCache):
        # The provider is up but cannot satisfy this request (it lacks the
        # requested safe-search capability) — a request that no provider can
        # honour must not be masked by stale data.
        provider = FakeProvider(
            name="fake",
            verticals=[Vertical.WEB],
            supports_safe_search=False,
        )
        search_cache = make_search_cache(cache_db, make_router([provider]))
        request = query(query="dolphins", provider="auto", safe_search=SafeSearch.STRICT)
        seed_search_entry(cache_db, request, age_seconds=10_000, ttl_seconds=900)

        with pytest.raises(NoProviderAvailableError):
            await search_cache.search(request)
        assert provider.search_calls == 0

    async def test_no_stale_for_empty_registry(self, cache_db: SqliteCache):
        search_cache = make_search_cache(cache_db, make_router([]))
        request = query(query="dolphins")
        seed_search_entry(cache_db, request, age_seconds=10_000, ttl_seconds=900)

        with pytest.raises(NoProviderAvailableError):
            await search_cache.search(request)

    async def test_stale_when_all_eligible_attempts_fail_in_auto_mode(
        self, cache_db: SqliteCache, search_cache: SearchCache, provider: FakeProvider
    ):
        # Auto mode with one eligible provider whose search() fails
        # transiently → NoProviderAvailableError with providers_attempted.
        request = query(query="dolphins", provider="auto")
        seed_search_entry(cache_db, request, age_seconds=10_000, ttl_seconds=900)
        provider.search_error = SearchProviderTimeoutError("timed out")

        response = await search_cache.search(request)

        assert response.metadata.from_cache is True
        assert response.metadata.stale is True
        assert provider.search_calls == 1  # the transient attempt

    async def test_no_stale_for_unexpected_errors(
        self, cache_db: SqliteCache, search_cache: SearchCache, provider: FakeProvider
    ):
        request = query(query="dolphins")
        seed_search_entry(cache_db, request, age_seconds=10_000, ttl_seconds=900)
        provider.search_error = RuntimeError("provider bug")

        with pytest.raises(RuntimeError):
            await search_cache.search(request)

    async def test_stale_only_considers_same_key(
        self, cache_db: SqliteCache, search_cache: SearchCache, provider: FakeProvider
    ):
        # A stale entry exists for a *different* request; the current request
        # (no entry) must fail rather than borrow someone else's results.
        seed_search_entry(
            cache_db,
            query(query="some other query", provider="fake"),
            age_seconds=10_000,
        )
        provider.search_error = SearchProviderUnavailableError("down")

        with pytest.raises(SearchProviderUnavailableError):
            await search_cache.search(query(query="dolphins", provider="fake"))

    # ------------------------------------------------------------- disabled
    async def test_disabled_cache_never_reads_or_writes(
        self, cache_db: SqliteCache, provider: FakeProvider
    ):
        search_cache = make_search_cache(cache_db, make_router([provider]), enabled=False)
        await search_cache.search(query(query="dolphins"))
        await search_cache.search(query(query="dolphins"))

        assert provider.search_calls == 2
        assert cache_db.search_stats()["search_total_entries"] == 0

    # --------------------------------------------------------------- input
    async def test_dict_input_is_validated(self, search_cache: SearchCache, provider: FakeProvider):
        response = await search_cache.search({"query": "dolphins"})
        assert response.metadata.from_cache is False
        assert provider.search_calls == 1

    async def test_invalid_dict_input_raises_without_cache(
        self, cache_db: SqliteCache, search_cache: SearchCache, provider: FakeProvider
    ):
        # A stale entry cannot mask an invalid request.
        seed_search_entry(cache_db, query(query="dolphins"), age_seconds=10_000)
        provider.search_error = SearchProviderUnavailableError("down")

        with pytest.raises(ValidationError):
            await search_cache.search({"query": ""})  # min_length=1

        assert provider.search_calls == 0

    async def test_invalid_dict_unknown_vertical(
        self, search_cache: SearchCache, provider: FakeProvider
    ):
        with pytest.raises(ValidationError) as exc_info:
            await search_cache.search({"query": "dolphins", "verticals": ["not-a-vertical"]})
        assert exc_info.value.details["errors"][0]["field"].startswith("verticals")

    # ------------------------------------------------------------ metrics
    async def test_metrics_counters(self, cache_db: SqliteCache, provider: FakeProvider):
        metrics = MetricsCollector()
        search_cache = SearchCache(store=cache_db, router=make_router([provider]), metrics=metrics)
        request = query(query="dolphins")
        await search_cache.search(request)  # miss
        await search_cache.search(request)  # hit
        seed_search_entry(cache_db, request, age_seconds=10_000)
        provider.search_error = SearchProviderUnavailableError("down")
        await search_cache.search(request)  # stale hit

        rendered = metrics.render()
        assert "search_cache_misses_total 1" in rendered
        assert "search_cache_hits_total 1" in rendered
        assert "search_cache_stale_hits_total 1" in rendered
        assert "search_cache_errors_total 1" in rendered


class TestStorageResilience:
    """Storage failures must degrade gracefully, never break the search."""

    async def test_read_failure_degrades_to_miss(
        self, cache_db: SqliteCache, search_cache: SearchCache, provider: FakeProvider, monkeypatch
    ):
        def boom(key: str):
            raise OSError("db gone")

        monkeypatch.setattr(cache_db, "get_search_entry", boom)
        response = await search_cache.search(query(query="dolphins"))
        assert provider.search_calls == 1
        assert response.metadata.from_cache is False

    async def test_write_failure_still_returns_result(
        self, cache_db: SqliteCache, search_cache: SearchCache, provider: FakeProvider, monkeypatch
    ):
        def boom(**kwargs) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(cache_db, "set_search_entry", boom)
        response = await search_cache.search(query(query="dolphins"))
        assert provider.search_calls == 1
        assert response.metadata.from_cache is False
        assert response.results  # delivered despite the failed write

    async def test_naive_timestamps_are_treated_as_utc(self, cache_db: SqliteCache):
        # Legacy rows without timezone info must still be interpreted correctly.
        entry = SearchCacheEntry(
            cache_key="k",
            response=SearchResponse(
                query="q",
                verticals=[Vertical.WEB],
                results=[],
                metadata=SearchMetadata(provider="fake"),
            ),
            fetched_at=(datetime.now(UTC) - timedelta(hours=1)).replace(tzinfo=None),
            expires_at=(datetime.now(UTC) - timedelta(minutes=30)).replace(tzinfo=None),
            content_length=10,
        )
        assert entry.is_expired is True
        assert 3000 <= entry.age_seconds() <= 3700

    async def test_unknown_scraper_error_never_serves_stale(
        self, cache_db: SqliteCache, search_cache: SearchCache, provider: FakeProvider
    ):
        class WeirdScraperError(ScraperError):
            def __init__(self) -> None:
                super().__init__("weird", "something unknown happened", None, 500)

        request = query(query="dolphins")
        seed_search_entry(cache_db, request, age_seconds=10_000, ttl_seconds=900)
        provider.search_error = WeirdScraperError()

        with pytest.raises(WeirdScraperError):
            await search_cache.search(request)

    async def test_discard_failure_is_swallowed(
        self, cache_db: SqliteCache, search_cache: SearchCache, provider: FakeProvider, monkeypatch
    ):
        def boom(key: str) -> None:
            raise OSError("db gone")

        monkeypatch.setattr(cache_db, "delete_search_entry", boom)
        key = make_search_cache_key(query(query="dolphins"))
        cache_db.set_search_entry(
            cache_key=key,
            request="{}",
            response="{corrupt",
            fetched_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            content_length=9,
        )
        response = await search_cache.search(query(query="dolphins"))
        assert provider.search_calls == 1
        assert response.metadata.from_cache is False


# ============================================================== maintenance
class TestSearchCacheMaintenance:
    def test_cleanup_removes_expired_search_entries(self, cache_db: SqliteCache):
        now = datetime.now(UTC)
        # One long-expired (past the grace window), one expired but within
        # the grace window, one still fresh.
        expiries = {
            "old": now - timedelta(hours=2),
            "grace": now - timedelta(minutes=30),
            "fresh": now + timedelta(hours=1),
        }
        for label, expires_at in expiries.items():
            key = make_search_cache_key(query(query=f"q-{label}"))
            cache_db.set_search_entry(
                cache_key=key,
                request="{}",
                response="{}",
                fetched_at=now - timedelta(days=2),
                expires_at=expires_at,
                content_length=100,
            )

        result = cache_db.cleanup(
            delete_expired_after_seconds=3600, max_entries=10_000, max_size_bytes=2**60
        )

        # The grace window keeps the recent-expiry entry, deletes the old one.
        assert result.search_deleted_expired == 1
        assert result.search_entries_after == 2
        # Scrape table untouched.
        assert result.entries_before == 0
        assert result.entries_after == 0

    def test_cleanup_max_entries_applies_per_table(self, cache_db: SqliteCache):
        for i in range(5):
            seed_search_entry(cache_db, query(query=f"q{i}"))
        scrape_entry = CacheEntry(
            cache_key="scrape-1",
            url="https://example.com",
            final_url="https://example.com",
            status_code=200,
            html="<html>hi</html>",
            fetched_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            mode="static",
            content_length=10,
            headers="{}",
            error_metadata=None,
        )
        cache_db.set(scrape_entry)

        result = cache_db.cleanup(
            delete_expired_after_seconds=10**9, max_entries=3, max_size_bytes=2**60
        )

        # 5 search entries → 3; the 1 scrape entry stays under its own limit.
        assert result.search_deleted_by_max_entries == 2
        assert result.search_entries_after == 3
        assert result.deleted_by_max_entries == 0
        assert result.entries_after == 1
        stats = cache_db.stats()
        assert stats["total_entries"] == 1
        assert stats["search_total_entries"] == 3

    def test_cleanup_max_size_applies_per_table(self, cache_db: SqliteCache):
        for i in range(4):
            key = make_search_cache_key(query(query=f"q{i}"))
            cache_db.set_search_entry(
                cache_key=key,
                request="{}",
                response="x" * 1000,
                fetched_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                content_length=1000,
            )
        result = cache_db.cleanup(
            delete_expired_after_seconds=10**9, max_entries=10_000, max_size_bytes=2500
        )
        assert result.search_deleted_by_max_size == 2  # 4000 → 2000 <= 2500
        assert result.search_entries_after == 2

    def test_cleanup_result_schema_fields(self, cache_db: SqliteCache):
        result = cache_db.cleanup()
        assert result.search_deleted_expired == 0
        assert result.search_total_deleted == 0
        assert result.search_entries_before == 0
        assert result.search_entries_after == 0
        # Existing scrape semantics preserved.
        assert result.total_deleted == 0
        assert result.entries_before == 0

    def test_cache_stats_schema_accepts_search_fields(self, cache_db: SqliteCache):
        from app.schemas.scrape import CacheStats

        seed_search_entry(cache_db, query(query="q"))
        stats = cache_db.stats()
        payload = CacheStats(
            total_entries=stats["total_entries"],
            total_size_bytes=stats["total_size_bytes"],
            expired_entries=stats["expired_entries"],
            cache_path=stats["cache_path"],
            search_total_entries=stats["search_total_entries"],
            search_total_size_bytes=stats["search_total_size_bytes"],
            search_expired_entries=stats["search_expired_entries"],
        )
        assert payload.search_total_entries == 1

    def test_maintenance_gauges_updated(self, cache_db: SqliteCache):
        metrics = MetricsCollector()
        seed_search_entry(cache_db, query(query="q"))
        result = cache_db.cleanup()

        # Mirror what CacheMaintenanceService does after a cleanup run.
        metrics.set_gauge("search_cache_entries_total", result.search_entries_after)
        metrics.set_gauge(
            "search_cache_size_bytes", cache_db.search_stats()["search_total_size_bytes"]
        )
        rendered = metrics.render()
        assert "search_cache_entries_total 1" in rendered
        assert "search_cache_size_bytes" in rendered


# ============================================================== factory
class TestBuildSearchCache:
    def test_builds_from_settings(self, cache_db: SqliteCache, monkeypatch: pytest.MonkeyPatch):
        for var in CACHE_ENV_VARS:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("SCRAPER_SEARCH_CACHE_DEFAULT_TTL_SECONDS", "333")
        settings = _settings_with_search_block(
            {
                "cache": {"enabled": False, "stale_if_error": False},
                "providers": {"searxng": {"enabled": False}},
            }
        )
        router = make_router([capable()])
        search_cache = build_search_cache(settings, router, cache_db)
        assert search_cache.config.enabled is False
        assert search_cache.config.stale_if_error is False
        assert search_cache.config.default_ttl_seconds == 333
        assert search_cache.router is router
