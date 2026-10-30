"""Tests for the SearXNG vertical/category/safe-search/time-range mappings."""

import pytest

from app.search.models import SafeSearch, TimeRange, Vertical
from app.search.providers.searxng.mapping import (
    SAFE_SEARCH_LEVELS,
    TIME_RANGE_PARAMS,
    VERTICAL_TO_CATEGORY,
    category_to_vertical,
    verticals_to_categories,
)


class TestVerticalsToCategories:
    def test_known_verticals(self):
        assert verticals_to_categories([Vertical.WEB]) == ["general"]
        assert verticals_to_categories([Vertical.WEB, Vertical.NEWS, Vertical.MAPS]) == [
            "general",
            "news",
            "map",
        ]

    def test_all_verticals_have_a_category(self):
        # Every domain vertical must map to some SearXNG category.
        for vertical in Vertical:
            assert vertical in VERTICAL_TO_CATEGORY

    def test_empty_input(self):
        assert verticals_to_categories([]) == []


class TestCategoryToVertical:
    def test_known_categories(self):
        assert category_to_vertical("general") == Vertical.WEB
        assert category_to_vertical("news") == Vertical.NEWS
        assert category_to_vertical("map") == Vertical.MAPS

    def test_every_mapped_category_round_trips(self):
        for vertical, category in VERTICAL_TO_CATEGORY.items():
            assert category_to_vertical(category) == vertical

    @pytest.mark.parametrize("category", ["", "some-future-category", None, 42])
    def test_unknown_or_missing_falls_back_to_web(self, category):
        assert category_to_vertical(category) == Vertical.WEB


class TestSafeSearchLevels:
    def test_all_safe_search_levels_map_to_ints(self):
        assert SAFE_SEARCH_LEVELS[SafeSearch.OFF] == 0
        assert SAFE_SEARCH_LEVELS[SafeSearch.MODERATE] == 1
        assert SAFE_SEARCH_LEVELS[SafeSearch.STRICT] == 2

    def test_covers_all_domain_levels(self):
        assert set(SAFE_SEARCH_LEVELS) == set(SafeSearch)


class TestTimeRangeParams:
    def test_all_time_ranges_map(self):
        assert TIME_RANGE_PARAMS[TimeRange.DAY] == "day"
        assert TIME_RANGE_PARAMS[TimeRange.WEEK] == "week"
        assert TIME_RANGE_PARAMS[TimeRange.MONTH] == "month"
        assert TIME_RANGE_PARAMS[TimeRange.YEAR] == "year"

    def test_covers_all_domain_ranges(self):
        assert set(TIME_RANGE_PARAMS) == set(TimeRange)
