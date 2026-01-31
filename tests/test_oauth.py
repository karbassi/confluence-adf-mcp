"""Tests for OAuth 2.0 token refresh support."""

import asyncio
import json
import time

import httpx
import pytest
import respx

import server
from tests.factories import make_page_response

BASE = "https://test.atlassian.net/wiki"
TOKEN_URL = "https://auth.atlassian.com/oauth/token"


# ---------------------------------------------------------------------------
# _OAuthTokenManager — disk persistence and expiry
# ---------------------------------------------------------------------------


class TestOAuthTokenManager:
    def test_load_from_empty_disk(self, tmp_path):
        """Manager starts with env seed when no file exists."""
        mgr = server._OAuthTokenManager(
            client_id="cid",
            client_secret="csec",
            initial_refresh_token="seed-rt",
            token_file=tmp_path / "tokens.json",
        )
        assert mgr._refresh_token == "seed-rt"
        assert mgr._access_token == ""
        assert mgr.is_expired()

    def test_load_from_disk(self, tmp_path):
        """Manager restores persisted tokens on init."""
        token_file = tmp_path / "tokens.json"
        token_file.write_text(
            json.dumps(
                {
                    "refresh_token": "persisted-rt",
                    "access_token": "persisted-at",
                    "expires_at": time.time() + 3600,
                }
            )
        )
        mgr = server._OAuthTokenManager(
            client_id="cid",
            client_secret="csec",
            initial_refresh_token="seed-rt",
            token_file=token_file,
        )
        assert mgr._refresh_token == "persisted-rt"
        assert mgr._access_token == "persisted-at"
        assert not mgr.is_expired()

    def test_save_to_disk(self, tmp_path):
        """_save_to_disk writes the current state."""
        token_file = tmp_path / "sub" / "tokens.json"
        mgr = server._OAuthTokenManager(
            client_id="cid",
            client_secret="csec",
            initial_refresh_token="rt",
            token_file=token_file,
        )
        mgr._access_token = "at-123"
        mgr._refresh_token = "rt-456"
        mgr._expires_at = 9999999999.0
        mgr._save_to_disk()

        saved = json.loads(token_file.read_text())
        assert saved["access_token"] == "at-123"
        assert saved["refresh_token"] == "rt-456"
        assert saved["expires_at"] == 9999999999.0

    def test_is_expired_with_buffer(self, tmp_path):
        """Token is considered expired within the 5-minute buffer."""
        mgr = server._OAuthTokenManager(
            client_id="cid",
            client_secret="csec",
            initial_refresh_token="rt",
            token_file=tmp_path / "tokens.json",
        )
        # Expires in 4 minutes — should be treated as expired (buffer is 5 min)
        mgr._expires_at = time.time() + 240
        assert mgr.is_expired()

        # Expires in 6 minutes — should be treated as valid
        mgr._expires_at = time.time() + 360
        assert not mgr.is_expired()

    def test_load_corrupt_disk_file(self, tmp_path):
        """Manager gracefully handles a corrupt token file."""
        token_file = tmp_path / "tokens.json"
        token_file.write_text("not json{{{")
        mgr = server._OAuthTokenManager(
            client_id="cid",
            client_secret="csec",
            initial_refresh_token="seed-rt",
            token_file=token_file,
        )
        assert mgr._refresh_token == "seed-rt"
        assert mgr._access_token == ""

    def test_ensure_valid_returns_cached_token(self, tmp_path):
        """ensure_valid returns existing token when not expired."""
        mgr = server._OAuthTokenManager(
            client_id="cid",
            client_secret="csec",
            initial_refresh_token="rt",
            token_file=tmp_path / "tokens.json",
        )
        mgr._access_token = "good-token"
        mgr._expires_at = time.time() + 3600

        token = asyncio.get_event_loop().run_until_complete(mgr.ensure_valid())
        assert token == "good-token"


# ---------------------------------------------------------------------------
# OAuth refresh flow
# ---------------------------------------------------------------------------


class TestOAuthRefresh:
    @respx.mock
    async def test_refresh_success(self, tmp_path):
        """Successful refresh updates tokens and persists to disk."""
        token_file = tmp_path / "tokens.json"
        mgr = server._OAuthTokenManager(
            client_id="cid",
            client_secret="csec",
            initial_refresh_token="old-rt",
            token_file=token_file,
        )

        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "new-at",
                    "refresh_token": "new-rt",
                    "expires_in": 3600,
                },
            )
        )

        token = await mgr.ensure_valid()
        assert token == "new-at"
        assert mgr._refresh_token == "new-rt"

        # Verify persisted
        saved = json.loads(token_file.read_text())
        assert saved["access_token"] == "new-at"
        assert saved["refresh_token"] == "new-rt"

    @respx.mock
    async def test_refresh_without_rotating_token(self, tmp_path):
        """When server doesn't return a new refresh token, keep the old one."""
        mgr = server._OAuthTokenManager(
            client_id="cid",
            client_secret="csec",
            initial_refresh_token="original-rt",
            token_file=tmp_path / "tokens.json",
        )

        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "new-at",
                    "expires_in": 3600,
                },
            )
        )

        await mgr.ensure_valid()
        assert mgr._refresh_token == "original-rt"

    @respx.mock
    async def test_refresh_error(self, tmp_path):
        """Failed refresh raises OAuthRefreshError."""
        mgr = server._OAuthTokenManager(
            client_id="cid",
            client_secret="csec",
            initial_refresh_token="bad-rt",
            token_file=tmp_path / "tokens.json",
        )

        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(400, json={"error": "invalid_grant"})
        )

        with pytest.raises(server.OAuthRefreshError, match="400"):
            await mgr.ensure_valid()

    @respx.mock
    async def test_refresh_server_error(self, tmp_path):
        """5xx from token endpoint raises OAuthRefreshError."""
        mgr = server._OAuthTokenManager(
            client_id="cid",
            client_secret="csec",
            initial_refresh_token="rt",
            token_file=tmp_path / "tokens.json",
        )

        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )

        with pytest.raises(server.OAuthRefreshError, match="500"):
            await mgr.ensure_valid()

    @respx.mock
    async def test_concurrent_refresh_dedup(self, tmp_path):
        """Concurrent ensure_valid() calls result in only one refresh request."""
        mgr = server._OAuthTokenManager(
            client_id="cid",
            client_secret="csec",
            initial_refresh_token="rt",
            token_file=tmp_path / "tokens.json",
        )

        call_count = 0

        async def counting_side_effect(request, **kwargs):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)
            return httpx.Response(
                200,
                json={
                    "access_token": "concurrent-at",
                    "refresh_token": "concurrent-rt",
                    "expires_in": 3600,
                },
            )

        respx.post(TOKEN_URL).mock(side_effect=counting_side_effect)

        tokens = await asyncio.gather(
            mgr.ensure_valid(),
            mgr.ensure_valid(),
            mgr.ensure_valid(),
        )

        assert all(t == "concurrent-at" for t in tokens)
        # Lock should deduplicate: only 1 actual refresh
        assert call_count == 1

    @respx.mock
    async def test_token_rotation_persisted(self, tmp_path):
        """New refresh token from rotation is saved and used next time."""
        token_file = tmp_path / "tokens.json"
        mgr = server._OAuthTokenManager(
            client_id="cid",
            client_secret="csec",
            initial_refresh_token="rt-v1",
            token_file=token_file,
        )

        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "at-1",
                    "refresh_token": "rt-v2",
                    "expires_in": 3600,
                },
            )
        )

        await mgr.ensure_valid()

        # Load a new manager from disk — should pick up rotated token
        mgr2 = server._OAuthTokenManager(
            client_id="cid",
            client_secret="csec",
            initial_refresh_token="rt-v1",
            token_file=token_file,
        )
        assert mgr2._refresh_token == "rt-v2"


# ---------------------------------------------------------------------------
# _OAuthAuth — httpx Auth subclass
# ---------------------------------------------------------------------------


class TestOAuthAuth:
    @respx.mock
    async def test_bearer_header_injected(self, oauth_env, tmp_cache):
        """_OAuthAuth sets Authorization: Bearer header on requests."""
        # Pre-set a valid access token so no refresh is needed
        oauth_env._access_token = "my-bearer-token"
        oauth_env._expires_at = time.time() + 3600

        page = make_page_response()
        route = respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=page)
        )

        await server.confluence_get_page("12345")

        assert route.called
        request = route.calls[0].request
        assert request.headers["authorization"] == "Bearer my-bearer-token"

    @respx.mock
    async def test_tool_works_with_oauth(self, oauth_env, tmp_cache):
        """A tool call completes successfully in OAuth mode."""
        oauth_env._access_token = "valid-token"
        oauth_env._expires_at = time.time() + 3600

        page = make_page_response()
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=page)
        )

        result = await server.confluence_get_page("12345")
        assert "Test Page" in result.content[0].text


# ---------------------------------------------------------------------------
# Auth mode detection
# ---------------------------------------------------------------------------


class TestAuthModeDetection:
    def test_basic_auth_by_default(self):
        """Without OAuth env vars, _auth() returns BasicAuth."""
        # The test conftest sets up basic auth env, so default is basic
        auth = server._auth()
        assert isinstance(auth, httpx.BasicAuth)

    def test_oauth_auth_when_configured(self, oauth_env):
        """With OAuth env vars, _auth() returns _OAuthAuth."""
        auth = server._auth()
        assert isinstance(auth, server._OAuthAuth)


# ---------------------------------------------------------------------------
# Error handling in OAuth mode
# ---------------------------------------------------------------------------


class TestErrorHandlingOAuth:
    @respx.mock
    async def test_401_message_oauth_mode(self, oauth_env, tmp_cache):
        """401 error in OAuth mode shows OAuth-specific message."""
        oauth_env._access_token = "expired-token"
        oauth_env._expires_at = time.time() + 3600  # not expired per manager

        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(401, text="Unauthorized")
        )

        result = await server.confluence_get_page("12345")
        text = result.content[0].text
        assert "OAuth" in text
        assert "expired" in text.lower() or "invalid" in text.lower()

    @respx.mock
    async def test_refresh_error_surfaced(self, oauth_env, tmp_cache):
        """OAuthRefreshError is caught by _with_error_handling and surfaced."""
        # Token is expired, so ensure_valid() will try to refresh
        oauth_env._access_token = ""
        oauth_env._expires_at = 0

        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(400, json={"error": "invalid_grant"})
        )

        result = await server.confluence_get_page("12345")
        text = result.content[0].text
        assert "OAuth token refresh failed" in text
        assert "400" in text
