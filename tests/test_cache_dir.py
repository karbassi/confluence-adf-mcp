"""Tests for cache directory resolution."""

from pathlib import Path

import server


class TestDefaultCacheDir:
    def test_default_is_absolute_under_home(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CACHE_DIR", raising=False)
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        result = server._resolve_cache_dir()
        assert result.is_absolute()
        assert result == tmp_path / ".cache" / "confluence-adf-mcp"

    def test_default_ignores_cwd(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CACHE_DIR", raising=False)
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.chdir(tmp_path)
        assert not str(server._resolve_cache_dir()).startswith(str(tmp_path / ".cache"))

    def test_respects_absolute_xdg_cache_home(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CACHE_DIR", raising=False)
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
        assert server._resolve_cache_dir() == tmp_path / "xdg" / "confluence-adf-mcp"

    def test_ignores_relative_xdg_cache_home(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CACHE_DIR", raising=False)
        monkeypatch.setenv("XDG_CACHE_HOME", "relative/xdg")
        monkeypatch.setenv("HOME", str(tmp_path))
        assert server._resolve_cache_dir() == tmp_path / ".cache" / "confluence-adf-mcp"

    def test_cache_dir_env_override_is_made_absolute(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("CACHE_DIR", "rel/cache")
        result = server._resolve_cache_dir()
        assert result.is_absolute()
        assert result == (tmp_path / "rel" / "cache").resolve()

    def test_module_cache_dir_is_absolute(self):
        assert Path(server.CACHE_DIR).is_absolute()
