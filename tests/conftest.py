"""Shared fixtures for the Confluence ADF MCP test suite."""

import os

# Set env vars BEFORE importing server (it reads them at module level)
os.environ.setdefault("CONFLUENCE_URL", "https://test.atlassian.net/wiki")
os.environ.setdefault("CONFLUENCE_USERNAME", "test@example.com")
os.environ.setdefault("CONFLUENCE_API_TOKEN", "test-token")

import pytest

import server


@pytest.fixture()
def tmp_cache(tmp_path, monkeypatch):
    """Redirect server.CACHE_DIR to a temporary directory."""
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(server, "CACHE_DIR", cache_dir)
    return cache_dir


@pytest.fixture()
def oauth_env(tmp_path, monkeypatch):
    """Switch server into OAuth mode with a temporary token file.

    Sets up a fake _OAuthTokenManager and _OAuthAuth so that _auth()
    returns an OAuth bearer flow instead of basic auth.
    """
    token_file = tmp_path / ".oauth_tokens.json"
    manager = server._OAuthTokenManager(
        client_id="test-client-id",
        client_secret="test-client-secret",
        initial_refresh_token="test-refresh-token",
        token_file=token_file,
    )
    monkeypatch.setattr(server, "_USE_OAUTH", True)
    monkeypatch.setattr(server, "_oauth_manager", manager)
    monkeypatch.setattr(server, "_OAUTH_TOKEN_FILE", token_file)
    return manager
