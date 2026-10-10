"""Tests for the main() entry point and transport selection."""

import pytest

import server

ENV_VARS = (
    "CONFLUENCE_ADF_MCP_TRANSPORT",
    "CONFLUENCE_ADF_MCP_HOST",
    "CONFLUENCE_ADF_MCP_PORT",
    "CONFLUENCE_ADF_MCP_ALLOWED_HOSTS",
    "CONFLUENCE_ADF_MCP_ALLOWED_ORIGINS",
)


@pytest.fixture
def run_calls(monkeypatch, tmp_cache):
    calls = []
    monkeypatch.setattr(server.mcp, "run", lambda *a, **kw: calls.append((a, kw)))
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    # main() mutates mcp.settings; restore after each test
    for attr in ("host", "port", "transport_security"):
        monkeypatch.setattr(server.mcp.settings, attr, getattr(server.mcp.settings, attr))
    return calls


def test_defaults_to_stdio(run_calls):
    server.main()
    assert run_calls == [(("stdio",), {})]


def test_explicit_stdio(monkeypatch, run_calls):
    monkeypatch.setenv("CONFLUENCE_ADF_MCP_TRANSPORT", "stdio")
    server.main()
    assert run_calls == [(("stdio",), {})]


def test_http_defaults_to_loopback_with_dns_rebinding_protection(monkeypatch, run_calls):
    monkeypatch.setenv("CONFLUENCE_ADF_MCP_TRANSPORT", "http")
    server.main()
    assert run_calls == [(("streamable-http",), {})]
    settings = server.mcp.settings
    assert settings.host == "127.0.0.1"
    assert settings.port == 8506
    sec = settings.transport_security
    assert sec.enable_dns_rebinding_protection is True
    assert sec.allowed_hosts == ["127.0.0.1:8506", "localhost:8506"]
    assert sec.allowed_origins == ["http://127.0.0.1:8506", "http://localhost:8506"]


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_http_host_and_port_from_env(monkeypatch, run_calls, host):
    monkeypatch.setenv("CONFLUENCE_ADF_MCP_TRANSPORT", "http")
    monkeypatch.setenv("CONFLUENCE_ADF_MCP_HOST", host)
    monkeypatch.setenv("CONFLUENCE_ADF_MCP_PORT", "9000")
    server.main()
    assert server.mcp.settings.host == host
    assert server.mcp.settings.port == 9000
    assert "127.0.0.1:9000" in server.mcp.settings.transport_security.allowed_hosts


def test_ipv6_loopback_allows_bracketed_host(monkeypatch, run_calls):
    monkeypatch.setenv("CONFLUENCE_ADF_MCP_TRANSPORT", "http")
    monkeypatch.setenv("CONFLUENCE_ADF_MCP_HOST", "::1")
    server.main()
    sec = server.mcp.settings.transport_security
    assert "[::1]:8506" in sec.allowed_hosts
    assert "http://[::1]:8506" in sec.allowed_origins


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "100.72.126.89", "example.com", ""])
def test_http_refuses_non_loopback_host(monkeypatch, run_calls, host):
    # No auth on the endpoint: anything that can reach it acts as the token owner.
    monkeypatch.setenv("CONFLUENCE_ADF_MCP_TRANSPORT", "http")
    monkeypatch.setenv("CONFLUENCE_ADF_MCP_HOST", host)
    with pytest.raises(SystemExit):
        server.main()
    assert run_calls == []


@pytest.mark.parametrize("port", ["abc", "0", "65536", "-1"])
def test_http_refuses_bad_port(monkeypatch, run_calls, port):
    monkeypatch.setenv("CONFLUENCE_ADF_MCP_TRANSPORT", "http")
    monkeypatch.setenv("CONFLUENCE_ADF_MCP_PORT", port)
    with pytest.raises(SystemExit):
        server.main()
    assert run_calls == []


def test_extra_allowed_hosts_and_origins(monkeypatch, run_calls):
    monkeypatch.setenv("CONFLUENCE_ADF_MCP_TRANSPORT", "http")
    monkeypatch.setenv("CONFLUENCE_ADF_MCP_ALLOWED_HOSTS", " mcp.ali.fyi , ,proxy.local:443")
    monkeypatch.setenv("CONFLUENCE_ADF_MCP_ALLOWED_ORIGINS", "https://mcp.ali.fyi")
    server.main()
    sec = server.mcp.settings.transport_security
    assert sec.allowed_hosts == [
        "127.0.0.1:8506", "localhost:8506", "mcp.ali.fyi", "proxy.local:443",
    ]
    assert sec.allowed_origins == [
        "http://127.0.0.1:8506", "http://localhost:8506", "https://mcp.ali.fyi",
    ]


def test_rejects_unknown_transport(monkeypatch, run_calls):
    monkeypatch.setenv("CONFLUENCE_ADF_MCP_TRANSPORT", "sse")
    with pytest.raises(SystemExit):
        server.main()
    assert run_calls == []


def test_console_script_points_at_main():
    import tomllib
    from pathlib import Path

    pyproject = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())
    assert pyproject["project"]["scripts"]["confluence-adf-mcp"] == "server:main"


def test_main_prunes_stale_sessions_at_startup(monkeypatch, run_calls):
    pruned = []
    monkeypatch.setattr(server, "_prune_stale_sessions", lambda **kw: pruned.append(kw) or 0)
    monkeypatch.setattr(server, "_last_prune", 0.0)
    server.main()
    assert len(pruned) == 1
