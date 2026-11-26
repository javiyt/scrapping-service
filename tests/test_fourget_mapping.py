"""Tests for the 4get mapping module (endpoint names, nsfw, epoch dates)."""

from datetime import UTC, datetime

from app.search.models import SafeSearch, Vertical
from app.search.providers.fourget.mapping import (
    ENDPOINT_TO_VERTICAL,
    NAME,
    OK_STATUS,
    PASS_COOKIE_NAME,
    RESULT_KEY,
    SAFE_SEARCH_TO_NSFW,
    SUPPORTED_VERTICALS,
    VERTICAL_TO_ENDPOINT,
    endpoint_for_vertical,
    epoch_to_datetime,
)


class TestEndpointMapping:
    def test_public_name_is_fourget(self):
        assert NAME == "4get"

    def test_supported_verticals_map_to_endpoints(self):
        assert VERTICAL_TO_ENDPOINT[Vertical.WEB] == "/api/v1/web"
        assert VERTICAL_TO_ENDPOINT[Vertical.NEWS] == "/api/v1/news"
        assert VERTICAL_TO_ENDPOINT[Vertical.IMAGES] == "/api/v1/images"
        assert VERTICAL_TO_ENDPOINT[Vertical.VIDEOS] == "/api/v1/videos"
        assert VERTICAL_TO_ENDPOINT[Vertical.MUSIC] == "/api/v1/music"

    def test_unsupported_verticals_have_no_endpoint(self):
        unsupported = (
            Vertical.SOCIAL,
            Vertical.IT,
            Vertical.SCIENCE,
            Vertical.MAPS,
            Vertical.FILES,
        )
        for vertical in unsupported:
            assert vertical not in VERTICAL_TO_ENDPOINT
            assert endpoint_for_vertical(vertical) is None

    def test_reverse_mapping_is_inverse(self):
        assert {path: vertical for vertical, path in ENDPOINT_TO_VERTICAL.items()} == {
            vertical: path for path, vertical in ENDPOINT_TO_VERTICAL.items()
        }
        for vertical in SUPPORTED_VERTICALS:
            assert ENDPOINT_TO_VERTICAL[VERTICAL_TO_ENDPOINT[vertical]] is vertical

    def test_supported_verticals_exactly_five(self):
        assert set(SUPPORTED_VERTICALS) == {
            Vertical.WEB,
            Vertical.NEWS,
            Vertical.IMAGES,
            Vertical.VIDEOS,
            Vertical.MUSIC,
        }
        assert len(SUPPORTED_VERTICALS) == 5


class TestEnvelopeConstants:
    def test_ok_status(self):
        assert OK_STATUS == "ok"

    def test_result_key(self):
        assert RESULT_KEY == "result"

    def test_pass_cookie_name(self):
        assert PASS_COOKIE_NAME == "pass"


class TestNsfwMapping:
    def test_levels(self):
        assert SAFE_SEARCH_TO_NSFW[SafeSearch.OFF] == "1"
        assert SAFE_SEARCH_TO_NSFW[SafeSearch.MODERATE] == "0"
        assert SAFE_SEARCH_TO_NSFW[SafeSearch.STRICT] == "0"


class TestEpochToDatetime:
    def test_int_epoch(self):
        assert epoch_to_datetime(1750000000) == datetime.fromtimestamp(1750000000, tz=UTC)

    def test_float_epoch(self):
        assert epoch_to_datetime(1750000000.5) == datetime.fromtimestamp(1750000000.5, tz=UTC)

    def test_numeric_string_epoch(self):
        assert epoch_to_datetime("1750000000") == datetime.fromtimestamp(1750000000, tz=UTC)

    def test_zero_epoch(self):
        assert epoch_to_datetime(0) == datetime(1970, 1, 1, tzinfo=UTC)

    def test_none_maps_to_none(self):
        assert epoch_to_datetime(None) is None

    def test_garbage_maps_to_none(self):
        assert epoch_to_datetime("not-a-date") is None
        assert epoch_to_datetime("12:30") is None

    def test_negative_maps_to_none(self):
        assert epoch_to_datetime(-5) is None

    def test_bool_maps_to_none(self):
        assert epoch_to_datetime(True) is None
        assert epoch_to_datetime(False) is None

    def test_nan_maps_to_none(self):
        assert epoch_to_datetime(float("nan")) is None

    def test_list_maps_to_none(self):
        assert epoch_to_datetime([1, 2]) is None
