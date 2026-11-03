"""Tests for the logical Camofox session abstraction.

Exercises the session's own behaviour (identity, registry, ownership, and
method delegation) through the real client backed by the offline
``MockCamofox`` transport from ``test_camofox_client``.
"""

import pytest

from app.camofox import CamofoxClient
from app.core.errors import ValidationError
from tests.test_camofox_client import MockCamofox, _config, _cookie


def _client_with_mock(mock: MockCamofox) -> CamofoxClient:
    return CamofoxClient(_config(), client=mock.client())


class TestSessionIdentity:
    def test_named_session(self):
        client = CamofoxClient(_config())
        session = client.create_session("Melissa's Shop")
        assert session.user_id == "scrapping-service-Melissa-s-Shop"
        assert len(session.session_key) == 32

    def test_anonymous_sessions_differ(self):
        client = CamofoxClient(_config())
        a = client.create_session()
        b = client.create_session()
        assert a.user_id != b.user_id
        assert a.session_key != b.session_key

    def test_explicit_identity_respected(self):
        client = CamofoxClient(_config())
        session = client.create_session(user_id="fixed-user", session_key="fixed-key")
        assert session.user_id == "fixed-user"
        assert session.session_key == "fixed-key"


class TestSessionRegistry:
    async def test_tabs_property_tracks_created_tabs(self):
        session = _client_with_mock(MockCamofox()).create_session()
        t1 = await session.create_tab()
        t2 = await session.create_tab()
        assert session.tabs == (t1, t2)

    async def test_close_tab_forgets(self):
        session = _client_with_mock(MockCamofox()).create_session()
        tab = await session.create_tab()
        await session.close_tab(tab)
        assert session.tabs == ()

    async def test_foreign_tab_is_refused(self):
        a = _client_with_mock(MockCamofox()).create_session()
        b = _client_with_mock(MockCamofox()).create_session()
        foreign = await b.create_tab()
        with pytest.raises(ValidationError):
            await a.close_tab(foreign)


class TestSessionDelegation:
    """Every session method delegates with the session's own identity."""

    async def test_click_type_evaluate_links(self):
        mock = MockCamofox()
        session = _client_with_mock(mock).create_session()
        tab = await session.create_tab()
        await session.click(tab, ref="e1")
        await session.type_text(tab, "hello", ref="e1")
        evaluate = await session.evaluate(tab, "1")
        links = await session.get_links(tab)
        snapshot = await session.snapshot(tab)
        cookies = await session.import_cookies([_cookie()])
        tabs = await session.list_tabs()
        await session.close_tab(tab)

        assert evaluate.ok is True
        assert len(links.links) == 2
        assert snapshot.snapshot
        assert cookies == 1
        assert len(tabs) == 1
        assert tab.url  # navigation result recorded on the tab

    async def test_session_close_is_idempotent_and_empties_registry(self):
        mock = MockCamofox()
        session = _client_with_mock(mock).create_session()
        await session.create_tab()
        await session.close()
        await session.close()  # still fine
        assert session.tabs == ()
        assert mock.last_request().url.path == f"/sessions/{session.user_id}"


async def test_close_tab_on_expired_tab_forgets_locally():
    """A tab the server no longer knows about is forgotten without error."""
    mock = MockCamofox()
    client = _client_with_mock(mock)
    session = client.create_session()
    tab = await client.create_tab(session)
    mock.status = 404
    mock.error = "Tab not found"
    await session.close_tab(tab)  # 404 upstream is swallowed
    assert session.tabs == ()
