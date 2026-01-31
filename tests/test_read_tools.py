"""Tests for read-only MCP tools."""

import json
from urllib.parse import unquote_plus

import httpx
import pytest
import respx

import server
from tests.factories import make_adf, make_page_response, make_paragraph, make_table

BASE = "https://test.atlassian.net/wiki"


# ---------------------------------------------------------------------------
# confluence_get_page
# ---------------------------------------------------------------------------

class TestGetPage:
    @respx.mock
    async def test_fetch_and_cache(self, tmp_cache):
        page = make_page_response()
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=page)
        )
        result = await server.confluence_get_page("12345")
        assert "Test Page" in result.content[0].text
        assert (tmp_cache / "12345.json").exists()

    @respx.mock
    async def test_url_resolve(self, tmp_cache):
        page = make_page_response(page_id="99")
        respx.get(f"{BASE}/api/v2/pages/99").mock(
            return_value=httpx.Response(200, json=page)
        )
        result = await server.confluence_get_page(f"{BASE}/spaces/X/pages/99/Title")
        assert "id=99" in result.content[0].text

    @respx.mock
    async def test_cache_structure(self, tmp_cache):
        page = make_page_response()
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=page)
        )
        await server.confluence_get_page("12345")
        cached = json.loads((tmp_cache / "12345.json").read_text())
        assert cached["id"] == "12345"
        assert cached["title"] == "Test Page"
        assert cached["version"] == 1
        assert "adf" in cached

    @respx.mock
    async def test_http_error(self, tmp_cache):
        respx.get(f"{BASE}/api/v2/pages/404").mock(
            return_value=httpx.Response(404)
        )
        result = await server.confluence_get_page("404")
        assert "Not found" in result.content[0].text


# ---------------------------------------------------------------------------
# confluence_search_pages
# ---------------------------------------------------------------------------

class TestSearchPages:
    @respx.mock
    async def test_text_wraps_cql(self):
        respx.get(f"{BASE}/rest/api/search").mock(
            return_value=httpx.Response(200, json={"results": [
                {"content": {"id": "1", "title": "Notes"}, "resultGlobalContainer": {"title": "Space"}, "excerpt": "some text"},
            ]})
        )
        result = await server.confluence_search_pages("meeting notes")
        assert "Found 1 result" in result.content[0].text
        req = respx.calls[0].request
        assert 'title~"meeting notes"' in unquote_plus(str(req.url))

    @respx.mock
    async def test_cql_passthrough(self):
        respx.get(f"{BASE}/rest/api/search").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        await server.confluence_search_pages('type=page AND title="exact"')
        req = respx.calls[0].request
        # CQL with operators should pass through, not be wrapped
        assert 'type%3Dpage' in str(req.url) or 'type=page' in str(req.url)

    @respx.mock
    async def test_empty_results(self):
        respx.get(f"{BASE}/rest/api/search").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        result = await server.confluence_search_pages("nothing")
        assert "No pages found" in result.content[0].text

    @respx.mock
    async def test_formatted_output(self):
        respx.get(f"{BASE}/rest/api/search").mock(
            return_value=httpx.Response(200, json={"results": [
                {"content": {"id": "10", "title": "A"}, "resultGlobalContainer": {"title": "S1"}, "excerpt": "<b>bold</b> text"},
                {"content": {"id": "20", "title": "B"}, "resultGlobalContainer": {"title": "S2"}, "excerpt": ""},
            ]})
        )
        result = await server.confluence_search_pages("query")
        text = result.content[0].text
        assert "[10]" in text
        assert "[20]" in text
        assert "bold text" in text  # HTML stripped


# ---------------------------------------------------------------------------
# confluence_list_pages
# ---------------------------------------------------------------------------

class TestListPages:
    @respx.mock
    async def test_formatted_list(self):
        respx.get(f"{BASE}/api/v2/spaces/SP1/pages").mock(
            return_value=httpx.Response(200, json={"results": [
                {"id": "1", "title": "Page A", "status": "current"},
                {"id": "2", "title": "Page B", "status": "draft"},
            ]})
        )
        result = await server.confluence_list_pages("SP1")
        text = result.content[0].text
        assert "2 page(s)" in text
        assert '[2] "Page B" [draft]' in text

    @respx.mock
    async def test_limit_cap(self):
        respx.get(f"{BASE}/api/v2/spaces/SP1/pages").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        await server.confluence_list_pages("SP1", limit=999)
        req = respx.calls[0].request
        assert "limit=250" in str(req.url)

    @respx.mock
    async def test_empty_space(self):
        respx.get(f"{BASE}/api/v2/spaces/SP1/pages").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        result = await server.confluence_list_pages("SP1")
        assert "No pages found" in result.content[0].text


# ---------------------------------------------------------------------------
# confluence_get_child_pages
# ---------------------------------------------------------------------------

class TestGetChildPages:
    @respx.mock
    async def test_children(self):
        respx.get(f"{BASE}/api/v2/pages/1/children").mock(
            return_value=httpx.Response(200, json={"results": [
                {"id": "2", "title": "Child A"},
                {"id": "3", "title": "Child B"},
            ]})
        )
        result = await server.confluence_get_child_pages("1")
        text = result.content[0].text
        assert "2 child page(s)" in text
        assert "Child A" in text

    @respx.mock
    async def test_no_children(self):
        respx.get(f"{BASE}/api/v2/pages/1/children").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        result = await server.confluence_get_child_pages("1")
        assert "No child pages" in result.content[0].text


# ---------------------------------------------------------------------------
# confluence_get_ancestors
# ---------------------------------------------------------------------------

class TestGetAncestors:
    @respx.mock
    async def test_ancestor_chain(self):
        respx.get(f"{BASE}/api/v2/pages/5/ancestors").mock(
            return_value=httpx.Response(200, json={"results": [
                {"id": "1", "title": "Root"},
                {"id": "3", "title": "Parent"},
            ]})
        )
        result = await server.confluence_get_ancestors("5")
        text = result.content[0].text
        assert "2 ancestor(s)" in text
        assert "Root" in text
        assert "Parent" in text

    @respx.mock
    async def test_root_page(self):
        respx.get(f"{BASE}/api/v2/pages/1/ancestors").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        result = await server.confluence_get_ancestors("1")
        assert "root-level page" in result.content[0].text


# ---------------------------------------------------------------------------
# confluence_get_labels
# ---------------------------------------------------------------------------

class TestGetLabels:
    @respx.mock
    async def test_labels(self):
        respx.get(f"{BASE}/api/v2/pages/1/labels").mock(
            return_value=httpx.Response(200, json={"results": [
                {"name": "important"}, {"name": "reviewed"},
            ]})
        )
        result = await server.confluence_get_labels("1")
        text = result.content[0].text
        assert "2 label(s)" in text
        assert "important" in text

    @respx.mock
    async def test_no_labels(self):
        respx.get(f"{BASE}/api/v2/pages/1/labels").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        result = await server.confluence_get_labels("1")
        assert "No labels" in result.content[0].text


# ---------------------------------------------------------------------------
# confluence_list_versions
# ---------------------------------------------------------------------------

class TestListVersions:
    @respx.mock
    async def test_version_list(self):
        respx.get(f"{BASE}/api/v2/pages/1/versions").mock(
            return_value=httpx.Response(200, json={"results": [
                {"number": 1, "message": "Created", "authorId": "u1", "createdAt": "2025-01-01"},
                {"number": 2, "message": "", "authorId": "u2", "createdAt": "2025-01-02"},
            ]})
        )
        result = await server.confluence_list_versions("1")
        text = result.content[0].text
        assert "2 version(s)" in text
        assert "v1" in text
        assert '"Created"' in text

    @respx.mock
    async def test_no_versions(self):
        respx.get(f"{BASE}/api/v2/pages/1/versions").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        result = await server.confluence_list_versions("1")
        assert "No version history" in result.content[0].text


# ---------------------------------------------------------------------------
# confluence_extract_text
# ---------------------------------------------------------------------------

class TestExtractText:
    @respx.mock
    async def test_basic_extraction(self):
        page = make_page_response(title="My Page")
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=page)
        )
        result = await server.confluence_extract_text("12345")
        text = result.content[0].text
        assert "# My Page" in text
        assert "Hello world" in text

    @respx.mock
    async def test_complex_adf(self):
        adf = make_adf([
            make_paragraph("Intro"),
            make_table([["A", "B"], ["C", "D"]]),
        ])
        page = make_page_response(adf=adf)
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=page)
        )
        result = await server.confluence_extract_text("12345")
        text = result.content[0].text
        assert "Intro" in text
        assert "A\tB" in text


# ---------------------------------------------------------------------------
# confluence_list_comments
# ---------------------------------------------------------------------------

class TestListComments:
    @respx.mock
    async def test_comments_with_adf(self):
        comment_adf = make_adf([make_paragraph("Nice work!")])
        respx.get(f"{BASE}/api/v2/pages/1/footer-comments").mock(
            return_value=httpx.Response(200, json={"results": [
                {
                    "id": "c1",
                    "authorId": "u1",
                    "createdAt": "2025-01-01",
                    "body": {"atlas_doc_format": {"value": json.dumps(comment_adf)}},
                },
            ]})
        )
        result = await server.confluence_list_comments("1")
        text = result.content[0].text
        assert "1 comment(s)" in text
        assert "Nice work!" in text

    @respx.mock
    async def test_no_comments(self):
        respx.get(f"{BASE}/api/v2/pages/1/footer-comments").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        result = await server.confluence_list_comments("1")
        assert "No comments" in result.content[0].text


# ---------------------------------------------------------------------------
# confluence_compare_versions
# ---------------------------------------------------------------------------

class TestCompareVersions:
    @respx.mock
    async def test_diff_output(self):
        adf_v1 = make_adf([make_paragraph("Hello")])
        adf_v2 = make_adf([make_paragraph("Hello World")])
        route = respx.get(f"{BASE}/rest/api/content/1")
        route.side_effect = [
            httpx.Response(200, json={"body": {"atlas_doc_format": {"value": json.dumps(adf_v1)}}}),
            httpx.Response(200, json={"body": {"atlas_doc_format": {"value": json.dumps(adf_v2)}}}),
        ]
        result = await server.confluence_compare_versions("1", 1, 2)
        text = result.content[0].text
        assert "---" in text
        assert "+++" in text

    @respx.mock
    async def test_identical_versions(self):
        adf = make_adf([make_paragraph("Same")])
        route = respx.get(f"{BASE}/rest/api/content/1")
        route.side_effect = [
            httpx.Response(200, json={"body": {"atlas_doc_format": {"value": json.dumps(adf)}}}),
            httpx.Response(200, json={"body": {"atlas_doc_format": {"value": json.dumps(adf)}}}),
        ]
        result = await server.confluence_compare_versions("1", 1, 2)
        assert "No text differences" in result.content[0].text

    @respx.mock
    async def test_different_versions(self):
        adf_v1 = make_adf([make_paragraph("Old content")])
        adf_v2 = make_adf([make_paragraph("New content")])
        route = respx.get(f"{BASE}/rest/api/content/1")
        route.side_effect = [
            httpx.Response(200, json={"body": {"atlas_doc_format": {"value": json.dumps(adf_v1)}}}),
            httpx.Response(200, json={"body": {"atlas_doc_format": {"value": json.dumps(adf_v2)}}}),
        ]
        result = await server.confluence_compare_versions("1", 1, 2)
        text = result.content[0].text
        assert "-Old content" in text
        assert "+New content" in text


# ---------------------------------------------------------------------------
# confluence_list_attachments
# ---------------------------------------------------------------------------

class TestListAttachments:
    @respx.mock
    async def test_attachments_with_size(self):
        respx.get(f"{BASE}/api/v2/pages/1/attachments").mock(
            return_value=httpx.Response(200, json={"results": [
                {"id": "a1", "title": "doc.pdf", "mediaType": "application/pdf", "fileSize": 10240},
            ]})
        )
        result = await server.confluence_list_attachments("1")
        text = result.content[0].text
        assert "1 attachment(s)" in text
        assert "doc.pdf" in text
        assert "10.0 KB" in text

    @respx.mock
    async def test_no_attachments(self):
        respx.get(f"{BASE}/api/v2/pages/1/attachments").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        result = await server.confluence_list_attachments("1")
        assert "No attachments" in result.content[0].text


# ---------------------------------------------------------------------------
# confluence_get_contributors
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# confluence_list_spaces
# ---------------------------------------------------------------------------

class TestListSpaces:
    @respx.mock
    async def test_spaces_listed(self):
        respx.get(f"{BASE}/api/v2/spaces").mock(
            return_value=httpx.Response(200, json={"results": [
                {"id": "1", "name": "Engineering", "key": "ENG", "type": "global"},
                {"id": "2", "name": "Personal", "key": "~user", "type": "personal"},
            ]})
        )
        result = await server.confluence_list_spaces()
        text = result.content[0].text
        assert "2 space(s)" in text
        assert "Engineering" in text
        assert "key=ENG" in text

    @respx.mock
    async def test_no_spaces(self):
        respx.get(f"{BASE}/api/v2/spaces").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        result = await server.confluence_list_spaces()
        assert "No spaces found" in result.content[0].text

    @respx.mock
    async def test_type_filter(self):
        respx.get(f"{BASE}/api/v2/spaces").mock(
            return_value=httpx.Response(200, json={"results": [
                {"id": "1", "name": "Eng", "key": "ENG", "type": "global"},
            ]})
        )
        await server.confluence_list_spaces(type="global")
        req = respx.calls[0].request
        assert "type=global" in str(req.url)

    @respx.mock
    async def test_limit_cap(self):
        respx.get(f"{BASE}/api/v2/spaces").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        await server.confluence_list_spaces(limit=999)
        req = respx.calls[0].request
        assert "limit=250" in str(req.url)


# ---------------------------------------------------------------------------
# confluence_get_contributors
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# confluence_list_inline_comments
# ---------------------------------------------------------------------------

class TestListInlineComments:
    @respx.mock
    async def test_comments_listed(self):
        comment_adf = make_adf([make_paragraph("Fix this typo")])
        respx.get(f"{BASE}/api/v2/pages/1/inline-comments").mock(
            return_value=httpx.Response(200, json={"results": [
                {
                    "id": "ic1",
                    "authorId": "u1",
                    "createdAt": "2025-01-01",
                    "body": {"atlas_doc_format": {"value": json.dumps(comment_adf)}},
                    "properties": {"inline-marker-ref": {"value": "some text"}},
                },
            ]})
        )
        result = await server.confluence_list_inline_comments("1")
        text = result.content[0].text
        assert "1 inline comment(s)" in text
        assert "Fix this typo" in text
        assert "some text" in text

    @respx.mock
    async def test_no_inline_comments(self):
        respx.get(f"{BASE}/api/v2/pages/1/inline-comments").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        result = await server.confluence_list_inline_comments("1")
        assert "No inline comments" in result.content[0].text


# ---------------------------------------------------------------------------
# confluence_get_page_properties
# ---------------------------------------------------------------------------

class TestGetPageProperties:
    @respx.mock
    async def test_properties_listed(self):
        respx.get(f"{BASE}/api/v2/pages/1/properties").mock(
            return_value=httpx.Response(200, json={"results": [
                {"key": "status", "value": "reviewed", "version": {"number": 1}},
                {"key": "score", "value": {"total": 42}, "version": {"number": 3}},
            ]})
        )
        result = await server.confluence_get_page_properties("1")
        text = result.content[0].text
        assert "2 propert(ies)" in text
        assert "status = reviewed" in text
        assert "score" in text

    @respx.mock
    async def test_no_properties(self):
        respx.get(f"{BASE}/api/v2/pages/1/properties").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        result = await server.confluence_get_page_properties("1")
        assert "No properties" in result.content[0].text


# ---------------------------------------------------------------------------
# confluence_get_user
# ---------------------------------------------------------------------------

class TestGetUser:
    @respx.mock
    async def test_user_found(self):
        respx.get(f"{BASE}/rest/api/user").mock(
            return_value=httpx.Response(200, json={
                "displayName": "Alice Smith",
                "accountType": "atlassian",
                "email": "alice@example.com",
            })
        )
        result = await server.confluence_get_user("abc123")
        text = result.content[0].text
        assert "Alice Smith" in text
        assert "alice@example.com" in text

    @respx.mock
    async def test_user_not_found(self):
        respx.get(f"{BASE}/rest/api/user").mock(
            return_value=httpx.Response(404)
        )
        result = await server.confluence_get_user("unknown")
        assert "User not found" in result.content[0].text

    @respx.mock
    async def test_user_without_email(self):
        respx.get(f"{BASE}/rest/api/user").mock(
            return_value=httpx.Response(200, json={
                "displayName": "Bot",
                "accountType": "app",
            })
        )
        result = await server.confluence_get_user("bot-id")
        text = result.content[0].text
        assert "Bot" in text
        assert "app" in text


# ---------------------------------------------------------------------------
# confluence_get_contributors
# ---------------------------------------------------------------------------

class TestGetContributors:
    @respx.mock
    async def test_unique_authors(self):
        respx.get(f"{BASE}/api/v2/pages/1/versions").mock(
            return_value=httpx.Response(200, json={"results": [
                {"number": 1, "authorId": "u1"},
                {"number": 2, "authorId": "u2"},
                {"number": 3, "authorId": "u1"},  # duplicate
            ]})
        )
        result = await server.confluence_get_contributors("1")
        text = result.content[0].text
        assert "2 contributor(s)" in text
        assert "u1" in text
        assert "u2" in text

    @respx.mock
    async def test_no_versions(self):
        respx.get(f"{BASE}/api/v2/pages/1/versions").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        result = await server.confluence_get_contributors("1")
        assert "No version history" in result.content[0].text


# ---------------------------------------------------------------------------
# confluence_list_cache
# ---------------------------------------------------------------------------

class TestListCache:
    async def test_with_entries(self, tmp_cache):
        server._write_cache("1", {"id": "1", "title": "Page A", "version": 1, "adf": {}})
        server._write_cache("2", {"id": "2", "title": "Page B", "version": 2, "adf": {}})
        result = await server.confluence_list_cache()
        text = result.content[0].text
        assert "2 cached page(s)" in text
        assert "Page A" in text
        assert "Page B" in text

    async def test_empty(self, tmp_cache):
        result = await server.confluence_list_cache()
        assert "Cache is empty" in result.content[0].text


# ---------------------------------------------------------------------------
# confluence_clear_cache
# ---------------------------------------------------------------------------

class TestClearCache:
    async def test_clear_specific_page(self, tmp_cache):
        server._write_cache("1", {"id": "1", "title": "T", "version": 1, "adf": {}})
        result = await server.confluence_clear_cache("1")
        assert "Cleared cache for page 1" in result.content[0].text
        assert not (tmp_cache / "1.json").exists()

    async def test_clear_all(self, tmp_cache):
        server._write_cache("1", {"id": "1", "title": "T", "version": 1, "adf": {}})
        server._write_cache("2", {"id": "2", "title": "T", "version": 1, "adf": {}})
        result = await server.confluence_clear_cache()
        assert "Cleared 2 cached page(s)" in result.content[0].text

    async def test_clear_missing_page(self, tmp_cache):
        result = await server.confluence_clear_cache("999")
        assert "No cache found" in result.content[0].text

    async def test_clear_already_empty(self, tmp_cache):
        result = await server.confluence_clear_cache()
        assert "Cache is already empty" in result.content[0].text
