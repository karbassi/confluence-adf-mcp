"""Tests for async helper functions that make HTTP calls."""

import json

import httpx
import pytest
import respx

import server
from tests.factories import make_adf, make_page_response, make_paragraph

BASE = "https://test.atlassian.net/wiki"


# ---------------------------------------------------------------------------
# _resolve_page_id
# ---------------------------------------------------------------------------

class TestResolvePageId:
    async def test_numeric_passthrough(self):
        async with httpx.AsyncClient() as client:
            result = await server._resolve_page_id(client, "12345")
        assert result == "12345"

    async def test_url_with_pages_id(self):
        async with httpx.AsyncClient() as client:
            result = await server._resolve_page_id(
                client, "https://test.atlassian.net/wiki/spaces/TEAM/pages/98765/My+Page"
            )
        assert result == "98765"

    @respx.mock
    async def test_tiny_url_redirect(self):
        tiny_url = "https://test.atlassian.net/wiki/x/BwD5O"
        final_url = f"{BASE}/spaces/TEAM/pages/55555/Title"
        # Simulate redirect: first request returns 302, second returns 200
        respx.get(tiny_url).mock(
            return_value=httpx.Response(302, headers={"Location": final_url})
        )
        respx.get(final_url).mock(
            return_value=httpx.Response(200)
        )
        async with httpx.AsyncClient() as client:
            result = await server._resolve_page_id(client, tiny_url)
        assert result == "55555"

    async def test_invalid_url_raises(self):
        async with httpx.AsyncClient() as client:
            with pytest.raises(ValueError, match="Could not resolve page ID"):
                await server._resolve_page_id(client, "not-a-url")

    @respx.mock
    async def test_http_error(self):
        url = "https://test.atlassian.net/wiki/x/bad"
        respx.get(url).mock(return_value=httpx.Response(404))
        async with httpx.AsyncClient() as client:
            with pytest.raises(httpx.HTTPStatusError):
                await server._resolve_page_id(client, url)


# ---------------------------------------------------------------------------
# _get_page_raw
# ---------------------------------------------------------------------------

class TestGetPageRaw:
    @respx.mock
    async def test_success(self):
        page_data = make_page_response()
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=page_data)
        )
        async with httpx.AsyncClient() as client:
            result = await server._get_page_raw(client, "12345")
        assert result["id"] == "12345"
        assert result["title"] == "Test Page"

    @respx.mock
    async def test_correct_params(self):
        respx.get(f"{BASE}/api/v2/pages/99").mock(
            return_value=httpx.Response(200, json=make_page_response(page_id="99"))
        )
        async with httpx.AsyncClient() as client:
            await server._get_page_raw(client, "99")
        req = respx.calls[0].request
        assert "body-format=atlas_doc_format" in str(req.url)

    @respx.mock
    async def test_http_error(self):
        respx.get(f"{BASE}/api/v2/pages/404").mock(
            return_value=httpx.Response(404)
        )
        async with httpx.AsyncClient() as client:
            with pytest.raises(httpx.HTTPStatusError):
                await server._get_page_raw(client, "404")


# ---------------------------------------------------------------------------
# _push_page_update
# ---------------------------------------------------------------------------

class TestPushPageUpdate:
    @respx.mock
    async def test_success(self):
        result_data = {"id": "1", "title": "T", "version": {"number": 2}}
        respx.put(f"{BASE}/api/v2/pages/1").mock(
            return_value=httpx.Response(200, json=result_data)
        )
        async with httpx.AsyncClient() as client:
            result = await server._push_page_update(client, "1", "T", {"doc": 1}, 1)
        assert result["version"]["number"] == 2

    @respx.mock
    async def test_correct_payload(self):
        adf = make_adf([make_paragraph("test")])
        respx.put(f"{BASE}/api/v2/pages/1").mock(
            return_value=httpx.Response(200, json={"id": "1", "title": "T", "version": {"number": 2}})
        )
        async with httpx.AsyncClient() as client:
            await server._push_page_update(client, "1", "Title", adf, 1, "msg")
        body = json.loads(respx.calls[0].request.content)
        assert body["id"] == "1"
        assert body["title"] == "Title"
        assert body["version"]["number"] == 2
        assert body["version"]["message"] == "msg"
        assert json.loads(body["body"]["value"]) == adf

    @respx.mock
    async def test_409_retry_success(self):
        """On 409 conflict, should re-fetch version and retry."""
        # First PUT returns 409
        put_route = respx.put(f"{BASE}/api/v2/pages/1")
        put_route.side_effect = [
            httpx.Response(409),
            httpx.Response(200, json={"id": "1", "title": "T", "version": {"number": 4}}),
        ]
        # GET to fetch current version
        respx.get(f"{BASE}/api/v2/pages/1").mock(
            return_value=httpx.Response(200, json=make_page_response(version=3))
        )
        async with httpx.AsyncClient() as client:
            result = await server._push_page_update(client, "1", "T", {}, 1)
        assert result["version"]["number"] == 4
        # Should have made 2 PUT calls
        put_calls = [c for c in respx.calls if c.request.method == "PUT"]
        assert len(put_calls) == 2

    @respx.mock
    async def test_409_retry_failure(self):
        """If retry also fails, should raise."""
        put_route = respx.put(f"{BASE}/api/v2/pages/1")
        put_route.side_effect = [
            httpx.Response(409),
            httpx.Response(409),
        ]
        respx.get(f"{BASE}/api/v2/pages/1").mock(
            return_value=httpx.Response(200, json=make_page_response(version=3))
        )
        async with httpx.AsyncClient() as client:
            with pytest.raises(httpx.HTTPStatusError):
                await server._push_page_update(client, "1", "T", {}, 1)

    @respx.mock
    async def test_auth_header(self):
        respx.put(f"{BASE}/api/v2/pages/1").mock(
            return_value=httpx.Response(200, json={"id": "1", "title": "T", "version": {"number": 2}})
        )
        async with httpx.AsyncClient() as client:
            await server._push_page_update(client, "1", "T", {}, 1)
        req = respx.calls[0].request
        assert "authorization" in {k.lower() for k in req.headers.keys()}


# ---------------------------------------------------------------------------
# _get_page_version_adf
# ---------------------------------------------------------------------------

class TestGetPageVersionAdf:
    @respx.mock
    async def test_correct_v1_params(self):
        adf = make_adf([make_paragraph("v2 content")])
        resp_data = {"body": {"atlas_doc_format": {"value": json.dumps(adf)}}}
        respx.get(f"{BASE}/rest/api/content/1").mock(
            return_value=httpx.Response(200, json=resp_data)
        )
        async with httpx.AsyncClient() as client:
            await server._get_page_version_adf(client, "1", 2)
        req = respx.calls[0].request
        assert "version=2" in str(req.url)
        assert "expand=body.atlas_doc_format" in str(req.url)

    @respx.mock
    async def test_adf_parsing(self):
        adf = make_adf([make_paragraph("old content")])
        resp_data = {"body": {"atlas_doc_format": {"value": json.dumps(adf)}}}
        respx.get(f"{BASE}/rest/api/content/1").mock(
            return_value=httpx.Response(200, json=resp_data)
        )
        async with httpx.AsyncClient() as client:
            result = await server._get_page_version_adf(client, "1", 1)
        assert result == adf
