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


class TestPageIdSanitization:
    BAD_IDS = [
        "../etc/passwd",
        "../../x",
        "/abs/path",
        "12/34",
        "12\\34",
        "..",
        "",
        "12345.json",
        "12 34",
        "١٢",  # Arabic-Indic digits pass str.isdigit() but are not ASCII
        "12\x00",
    ]

    def test_numeric_id_accepted(self, tmp_cache):
        assert server._cache_path("12345").name == "12345.json"

    def test_malicious_ids_rejected_by_cache_path(self, tmp_cache):
        for bad in self.BAD_IDS:
            try:
                server._cache_path(bad)
            except server.InvalidPageIdError:
                continue
            raise AssertionError(f"accepted {bad!r}")

    def test_write_cache_never_escapes_cache_dir(self, tmp_cache, tmp_path):
        for bad in self.BAD_IDS:
            try:
                server._write_cache(bad, {"x": 1})
            except server.InvalidPageIdError:
                pass
        assert not (tmp_path / "x.json").exists()
        assert not (tmp_path / "passwd.json").exists()

    async def test_edit_page_reports_invalid_id(self, tmp_cache):
        result = await server.confluence_edit_page("../../evil", "a", "b")
        assert "Invalid page ID" in result.content[0].text

    async def test_push_page_reports_invalid_id(self, tmp_cache):
        result = await server.confluence_push_page("../../evil")
        assert "Invalid page ID" in result.content[0].text

    async def test_clear_cache_reports_invalid_id(self, tmp_cache, tmp_path):
        victim = tmp_path / "victim.json"
        victim.write_text("{}")
        result = await server.confluence_clear_cache("../victim")
        assert "Invalid page ID" in result.content[0].text
        assert victim.exists()
