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
