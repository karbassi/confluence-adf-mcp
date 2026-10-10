"""Per-session cache isolation (shared HTTP server safety)."""

import contextlib
import json

import httpx
import respx

import server
from mcp.server.lowlevel.server import request_ctx
from mcp.shared.context import RequestContext
from tests.factories import make_page_response

BASE = "https://test.atlassian.net/wiki"


class _FakeRequest:
    def __init__(self, headers):
        self.headers = headers


class _FakeSession:
    """Stand-in for a ServerSession (only identity matters)."""


@contextlib.contextmanager
def as_session(session, session_id=None):
    """Run the body as if inside a tool call from ``session``."""
    headers = {"mcp-session-id": session_id} if session_id else {}
    ctx = RequestContext(
        request_id=1,
        meta=None,
        session=session,
        lifespan_context=None,
        request=_FakeRequest(headers) if session_id is not None else None,
    )
    token = request_ctx.set(ctx)
    try:
        yield
    finally:
        request_ctx.reset(token)


def _page(i):
    return {"id": str(i), "title": f"T{i}", "version": 1, "adf": {}}


class TestNamespace:
    def test_http_session_header_used(self, tmp_cache):
        with as_session(_FakeSession(), "abc123"):
            assert server._session_namespace() == "http-abc123"

    def test_bad_session_header_falls_back_to_session_object(self, tmp_cache):
        with as_session(_FakeSession(), "../../etc"):
            ns = server._session_namespace()
        assert "/" not in ns and ".." not in ns
        assert ns.startswith("session-")

    def test_same_session_object_is_stable(self, tmp_cache):
        s = _FakeSession()
        with as_session(s):
            a = server._session_namespace()
        with as_session(s):
            b = server._session_namespace()
        assert a == b

    def test_stdio_without_request_context_uses_process_namespace(self, tmp_cache):
        assert server._session_namespace() == server._PROCESS_NAMESPACE

    def test_cache_path_includes_namespace(self, tmp_cache):
        with as_session(_FakeSession(), "abc123"):
            path = server._cache_path("42")
        assert path == tmp_cache / "sessions" / "http-abc123" / "42.json"
        assert path.is_absolute()


class TestIsolation:
    def test_two_sessions_do_not_share_cache(self, tmp_cache):
        with as_session(_FakeSession(), "aaa"):
            server._write_cache("1", {**_page(1), "title": "from A"})
        with as_session(_FakeSession(), "bbb"):
            server._write_cache("1", {**_page(1), "title": "from B"})
        with as_session(_FakeSession(), "aaa"):
            assert server._read_cache("1")["title"] == "from A"
        with as_session(_FakeSession(), "bbb"):
            assert server._read_cache("1")["title"] == "from B"

    def test_other_session_cannot_read(self, tmp_cache):
        with as_session(_FakeSession(), "aaa"):
            server._write_cache("7", _page(7))
        with as_session(_FakeSession(), "bbb"):
            try:
                server._read_cache("7")
            except FileNotFoundError:
                pass
            else:
                raise AssertionError("session bbb read session aaa's cache")

    async def test_list_cache_scoped(self, tmp_cache):
        with as_session(_FakeSession(), "aaa"):
            server._write_cache("1", _page(1))
            server._write_cache("2", _page(2))
        with as_session(_FakeSession(), "bbb"):
            server._write_cache("3", _page(3))
            text = (await server.confluence_list_cache()).content[0].text
        assert "1 cached page(s)" in text
        assert "[3]" in text and "[1]" not in text

    async def test_clear_all_only_clears_caller(self, tmp_cache):
        with as_session(_FakeSession(), "aaa"):
            server._write_cache("1", _page(1))
            server._write_cache("2", _page(2))
        with as_session(_FakeSession(), "bbb"):
            server._write_cache("1", _page(1))
            text = (await server.confluence_clear_cache()).content[0].text
            assert "Cleared 1 cached page(s)" in text
        with as_session(_FakeSession(), "aaa"):
            assert server._read_cache("1") == _page(1)
            assert server._read_cache("2") == _page(2)

    async def test_clear_specific_only_clears_caller(self, tmp_cache):
        with as_session(_FakeSession(), "aaa"):
            server._write_cache("1", _page(1))
        with as_session(_FakeSession(), "bbb"):
            text = (await server.confluence_clear_cache("1")).content[0].text
            assert "No cache found" in text
        with as_session(_FakeSession(), "aaa"):
            assert server._read_cache("1") == _page(1)

    async def test_clear_all_keeps_oauth_token_file(self, tmp_cache):
        tmp_cache.mkdir(parents=True)
        token = tmp_cache / ".oauth_tokens.json"
        token.write_text("{}")
        with as_session(_FakeSession(), "aaa"):
            server._write_cache("1", _page(1))
            await server.confluence_clear_cache()
        assert token.exists()

    async def test_edit_in_one_session_does_not_touch_other(self, tmp_cache):
        doc = {
            "type": "doc", "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": "hello"}]}],
        }
        for sid in ("aaa", "bbb"):
            with as_session(_FakeSession(), sid):
                server._write_cache("5", {**_page(5), "adf": json.loads(json.dumps(doc))})
        with as_session(_FakeSession(), "aaa"):
            await server.confluence_edit_page("5", "hello", "changed")
            assert "changed" in json.dumps(server._read_cache("5")["adf"])
        with as_session(_FakeSession(), "bbb"):
            assert "hello" in json.dumps(server._read_cache("5")["adf"])

    @respx.mock
    async def test_get_page_returns_session_scoped_absolute_path(self, tmp_cache):
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=make_page_response())
        )
        with as_session(_FakeSession(), "aaa"):
            result = await server.confluence_get_page("12345")
        expected = tmp_cache / "sessions" / "http-aaa" / "12345.json"
        assert f"Cached at {expected}" in result.content[0].text
        assert expected.exists()
