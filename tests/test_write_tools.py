"""Tests for write (mutating) MCP tools."""

import json

import httpx
import pytest
import respx

import server
from tests.factories import (
    make_adf,
    make_mention,
    make_page_response,
    make_paragraph,
    make_table,
    make_task_list,
)

BASE = "https://test.atlassian.net/wiki"


def _push_result(page_id="12345", title="Test Page", version=2):
    """Standard push response."""
    return {"id": page_id, "title": title, "version": {"number": version}}


# ---------------------------------------------------------------------------
# confluence_edit_page
# ---------------------------------------------------------------------------

class TestEditPage:
    def _seed_cache(self, tmp_cache, page_id="1", adf=None):
        """Helper to seed a cached page."""
        adf = adf or make_adf([make_paragraph("Hello world")])
        data = {"id": page_id, "title": "T", "version": 1, "spaceId": "S", "adf": adf}
        server._write_cache(page_id, data)
        return data

    async def test_replace_in_cache(self, tmp_cache):
        self._seed_cache(tmp_cache)
        result = await server.confluence_edit_page("1", "Hello", "Goodbye")
        assert "1 replacement(s)" in result.content[0].text
        cached = server._read_cache("1")
        text = server._extract_text_from_adf(cached["adf"])
        assert "Goodbye world" in text

    async def test_replace_all(self, tmp_cache):
        adf = make_adf([make_paragraph("foo foo foo")])
        self._seed_cache(tmp_cache, adf=adf)
        result = await server.confluence_edit_page("1", "foo", "bar", replace_all=True)
        assert "3 replacement(s)" in result.content[0].text
        cached = server._read_cache("1")
        assert "bar bar bar" in server._extract_text_from_adf(cached["adf"])

    async def test_replace_first_only(self, tmp_cache):
        adf = make_adf([make_paragraph("foo foo foo")])
        self._seed_cache(tmp_cache, adf=adf)
        result = await server.confluence_edit_page("1", "foo", "bar", replace_all=False)
        assert "1 replacement(s)" in result.content[0].text

    async def test_not_found(self, tmp_cache):
        self._seed_cache(tmp_cache)
        result = await server.confluence_edit_page("1", "MISSING", "x")
        assert "Text not found" in result.content[0].text

    async def test_structural_nodes_untouched(self, tmp_cache):
        adf = make_adf([
            {"type": "heading", "attrs": {"level": 1}, "content": [{"type": "text", "text": "Title"}]},
            make_paragraph("Hello world"),
        ])
        self._seed_cache(tmp_cache, adf=adf)
        await server.confluence_edit_page("1", "Hello", "Goodbye")
        cached = server._read_cache("1")
        # heading node type should remain
        assert cached["adf"]["content"][0]["type"] == "heading"


# ---------------------------------------------------------------------------
# confluence_push_page
# ---------------------------------------------------------------------------

class TestPushPage:
    @respx.mock
    async def test_read_cache_push_update_cache(self, tmp_cache):
        adf = make_adf([make_paragraph("content")])
        data = {"id": "1", "title": "T", "version": 1, "spaceId": "S", "adf": adf}
        server._write_cache("1", data)

        respx.put(f"{BASE}/api/v2/pages/1").mock(
            return_value=httpx.Response(200, json=_push_result("1", "T", 2))
        )
        result = await server.confluence_push_page("1")
        assert "v2" in result.content[0].text
        # Cache should be updated with new version
        cached = server._read_cache("1")
        assert cached["version"] == 2

    async def test_missing_cache(self, tmp_cache):
        result = await server.confluence_push_page("nonexistent")
        assert "No cached page for nonexistent" in result.content[0].text

    @respx.mock
    async def test_api_error(self, tmp_cache):
        data = {"id": "1", "title": "T", "version": 1, "spaceId": "S", "adf": {}}
        server._write_cache("1", data)
        respx.put(f"{BASE}/api/v2/pages/1").mock(
            return_value=httpx.Response(500)
        )
        result = await server.confluence_push_page("1")
        assert "server error" in result.content[0].text


# ---------------------------------------------------------------------------
# confluence_find_replace
# ---------------------------------------------------------------------------

class TestFindReplace:
    @respx.mock
    async def test_end_to_end(self, tmp_cache):
        page = make_page_response()
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=page)
        )
        respx.put(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=_push_result())
        )
        result = await server.confluence_find_replace("12345", "Hello", "Goodbye")
        text = result.content[0].text
        assert "Replaced 1 occurrence" in text
        assert "Goodbye" in text

    @respx.mock
    async def test_not_found_no_push(self, tmp_cache):
        page = make_page_response()
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=page)
        )
        result = await server.confluence_find_replace("12345", "MISSING", "x")
        assert "Text not found" in result.content[0].text
        # No PUT should have been made
        put_calls = [c for c in respx.calls if c.request.method == "PUT"]
        assert len(put_calls) == 0

    @respx.mock
    async def test_count_accuracy(self, tmp_cache):
        adf = make_adf([make_paragraph("aaa"), make_paragraph("aaa")])
        page = make_page_response(adf=adf)
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=page)
        )
        respx.put(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=_push_result())
        )
        result = await server.confluence_find_replace("12345", "a", "b")
        assert "Replaced 6 occurrence" in result.content[0].text

    @respx.mock
    async def test_replace_first_only(self, tmp_cache):
        adf = make_adf([make_paragraph("foo foo")])
        page = make_page_response(adf=adf)
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=page)
        )
        respx.put(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=_push_result())
        )
        result = await server.confluence_find_replace("12345", "foo", "bar", replace_all=False)
        assert "Replaced 1 occurrence" in result.content[0].text


# ---------------------------------------------------------------------------
# confluence_create_page
# ---------------------------------------------------------------------------

class TestCreatePage:
    @respx.mock
    async def test_correct_payload(self):
        adf = json.dumps(make_adf([make_paragraph("New page")]))
        respx.post(f"{BASE}/api/v2/pages").mock(
            return_value=httpx.Response(200, json={"id": "99", "title": "New", "version": {"number": 1}})
        )
        result = await server.confluence_create_page("SP1", "New", adf)
        assert "Created" in result.content[0].text
        body = json.loads(respx.calls[0].request.content)
        assert body["spaceId"] == "SP1"
        assert body["title"] == "New"

    @respx.mock
    async def test_with_parent_id(self):
        adf = json.dumps(make_adf())
        respx.post(f"{BASE}/api/v2/pages").mock(
            return_value=httpx.Response(200, json={"id": "99", "title": "Child", "version": {"number": 1}})
        )
        await server.confluence_create_page("SP1", "Child", adf, parent_id="50")
        body = json.loads(respx.calls[0].request.content)
        assert body["parentId"] == "50"

    @respx.mock
    async def test_success_message(self):
        adf = json.dumps(make_adf())
        respx.post(f"{BASE}/api/v2/pages").mock(
            return_value=httpx.Response(200, json={"id": "99", "title": "My Page", "version": {"number": 1}})
        )
        result = await server.confluence_create_page("SP1", "My Page", adf)
        assert "id=99" in result.content[0].text


# ---------------------------------------------------------------------------
# confluence_replace_mention
# ---------------------------------------------------------------------------

class TestReplaceMention:
    def _mention_page(self):
        adf = make_adf([{
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "Assigned to "},
                make_mention("@Alice", "alice-id"),
                {"type": "text", "text": " for review"},
            ],
        }])
        return make_page_response(adf=adf)

    @respx.mock
    async def test_success(self, tmp_cache):
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=self._mention_page())
        )
        respx.get(f"{BASE}/rest/api/search/user").mock(
            return_value=httpx.Response(200, json={"results": [
                {"user": {"accountId": "bob-id", "displayName": "Bob"}},
            ]})
        )
        respx.put(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=_push_result())
        )
        result = await server.confluence_replace_mention("12345", "Alice", "Bob")
        text = result.content[0].text
        assert "Replaced 1 mention" in text
        assert "Bob" in text

    @respx.mock
    async def test_user_not_found(self, tmp_cache):
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=self._mention_page())
        )
        respx.get(f"{BASE}/rest/api/search/user").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        result = await server.confluence_replace_mention("12345", "Alice", "Nobody")
        assert "User not found" in result.content[0].text

    @respx.mock
    async def test_multiple_users(self, tmp_cache):
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=self._mention_page())
        )
        respx.get(f"{BASE}/rest/api/search/user").mock(
            return_value=httpx.Response(200, json={"results": [
                {"user": {"accountId": "b1", "displayName": "Bob A"}},
                {"user": {"accountId": "b2", "displayName": "Bob B"}},
            ]})
        )
        result = await server.confluence_replace_mention("12345", "Alice", "Bob")
        text = result.content[0].text
        assert "Multiple users" in text
        assert "Bob A" in text
        assert "Bob B" in text

    @respx.mock
    async def test_no_mentions_matching(self, tmp_cache):
        # Page has mention of Alice but we search for "Charlie"
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=self._mention_page())
        )
        respx.get(f"{BASE}/rest/api/search/user").mock(
            return_value=httpx.Response(200, json={"results": [
                {"user": {"accountId": "bob-id", "displayName": "Bob"}},
            ]})
        )
        result = await server.confluence_replace_mention("12345", "Charlie", "Bob")
        assert "No mentions found" in result.content[0].text

    @respx.mock
    async def test_parallel_fetch(self, tmp_cache):
        """Page fetch and user search should both be called."""
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=self._mention_page())
        )
        respx.get(f"{BASE}/rest/api/search/user").mock(
            return_value=httpx.Response(200, json={"results": [
                {"user": {"accountId": "bob-id", "displayName": "Bob"}},
            ]})
        )
        respx.put(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=_push_result())
        )
        await server.confluence_replace_mention("12345", "Alice", "Bob")
        urls = [str(c.request.url) for c in respx.calls]
        assert any("api/v2/pages/12345" in u and "body-format" in u for u in urls)
        assert any("search/user" in u for u in urls)


# ---------------------------------------------------------------------------
# confluence_revert_page
# ---------------------------------------------------------------------------

class TestRevertPage:
    @respx.mock
    async def test_v1_restore_payload(self):
        respx.post(f"{BASE}/rest/api/content/1/version").mock(
            return_value=httpx.Response(200, json={"number": 5, "message": "Reverted"})
        )
        result = await server.confluence_revert_page("1", 3, "Rollback")
        body = json.loads(respx.calls[0].request.content)
        assert body["operationKey"] == "restore"
        assert body["params"]["versionNumber"] == 3
        assert body["params"]["message"] == "Rollback"

    @respx.mock
    async def test_success_message(self):
        respx.post(f"{BASE}/rest/api/content/1/version").mock(
            return_value=httpx.Response(200, json={"number": 5, "message": "Restored"})
        )
        result = await server.confluence_revert_page("1", 3)
        text = result.content[0].text
        assert "Reverted to v3" in text
        assert "v5" in text


# ---------------------------------------------------------------------------
# confluence_update_task
# ---------------------------------------------------------------------------

class TestUpdateTask:
    def _task_page(self):
        adf = make_adf([make_task_list([("Review PR", "TODO"), ("Write tests", "TODO")])])
        return make_page_response(adf=adf)

    @respx.mock
    async def test_done(self, tmp_cache):
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=self._task_page())
        )
        respx.put(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=_push_result())
        )
        result = await server.confluence_update_task("12345", "Review PR", "DONE")
        assert "Updated 1 task" in result.content[0].text
        assert "DONE" in result.content[0].text

    @respx.mock
    async def test_todo(self, tmp_cache):
        adf = make_adf([make_task_list([("Review PR", "DONE")])])
        page = make_page_response(adf=adf)
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=page)
        )
        respx.put(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=_push_result())
        )
        result = await server.confluence_update_task("12345", "Review PR", "TODO")
        assert "TODO" in result.content[0].text

    async def test_invalid_state(self):
        result = await server.confluence_update_task("12345", "x", "MAYBE")
        assert "Invalid state" in result.content[0].text

    @respx.mock
    async def test_no_match(self, tmp_cache):
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=self._task_page())
        )
        result = await server.confluence_update_task("12345", "Nonexistent", "DONE")
        assert "No task found" in result.content[0].text


# ---------------------------------------------------------------------------
# confluence_regex_replace
# ---------------------------------------------------------------------------

class TestRegexReplace:
    @respx.mock
    async def test_matches(self, tmp_cache):
        adf = make_adf([make_paragraph("date: 2024-01-15")])
        page = make_page_response(adf=adf)
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=page)
        )
        respx.put(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=_push_result())
        )
        result = await server.confluence_regex_replace("12345", r"\d{4}-\d{2}-\d{2}", "DATE")
        assert "Replaced 1 match" in result.content[0].text

    @respx.mock
    async def test_no_matches(self, tmp_cache):
        page = make_page_response()
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=page)
        )
        result = await server.confluence_regex_replace("12345", r"zzz\d+", "x")
        assert "No matches" in result.content[0].text

    async def test_invalid_regex(self):
        result = await server.confluence_regex_replace("12345", r"[invalid", "x")
        assert "Invalid regex" in result.content[0].text

    @respx.mock
    async def test_backreferences(self, tmp_cache):
        adf = make_adf([make_paragraph("Hello World")])
        page = make_page_response(adf=adf)
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=page)
        )
        respx.put(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=_push_result())
        )
        result = await server.confluence_regex_replace("12345", r"(Hello) (World)", r"\2 \1")
        assert "Replaced 1 match" in result.content[0].text


# ---------------------------------------------------------------------------
# confluence_update_table_cell
# ---------------------------------------------------------------------------

class TestUpdateTableCell:
    def _table_page(self):
        adf = make_adf([make_table([["A", "B"], ["C", "D"]])])
        return make_page_response(adf=adf)

    @respx.mock
    async def test_success(self, tmp_cache):
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=self._table_page())
        )
        respx.put(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=_push_result())
        )
        result = await server.confluence_update_table_cell("12345", 0, 1, "NEW")
        assert "Updated cell [0,1]" in result.content[0].text
        assert '"NEW"' in result.content[0].text

    @respx.mock
    async def test_row_oob(self, tmp_cache):
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=self._table_page())
        )
        result = await server.confluence_update_table_cell("12345", 10, 0, "X")
        assert "Row 10 out of range" in result.content[0].text

    @respx.mock
    async def test_col_oob(self, tmp_cache):
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=self._table_page())
        )
        result = await server.confluence_update_table_cell("12345", 0, 10, "X")
        assert "Column 10 out of range" in result.content[0].text

    @respx.mock
    async def test_no_tables(self, tmp_cache):
        page = make_page_response()  # No tables
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=page)
        )
        result = await server.confluence_update_table_cell("12345", 0, 0, "X")
        assert "No tables found" in result.content[0].text


# ---------------------------------------------------------------------------
# confluence_insert_table_row
# ---------------------------------------------------------------------------

class TestInsertTableRow:
    def _table_page(self):
        adf = make_adf([make_table([["A", "B"], ["C", "D"]])])
        return make_page_response(adf=adf)

    @respx.mock
    async def test_at_index(self, tmp_cache):
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=self._table_page())
        )
        respx.put(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=_push_result())
        )
        result = await server.confluence_insert_table_row("12345", 1, ["X", "Y"])
        assert "Inserted row at index 1" in result.content[0].text

    @respx.mock
    async def test_append_with_minus_one(self, tmp_cache):
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=self._table_page())
        )
        respx.put(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=_push_result())
        )
        result = await server.confluence_insert_table_row("12345", -1, ["X", "Y"])
        # Should append at end (index 2 since original had 2 rows)
        assert "Inserted row at index 2" in result.content[0].text

    @respx.mock
    async def test_beyond_range_appends(self, tmp_cache):
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=self._table_page())
        )
        respx.put(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=_push_result())
        )
        result = await server.confluence_insert_table_row("12345", 999, ["X"])
        assert "Inserted row at index 2" in result.content[0].text


# ---------------------------------------------------------------------------
# confluence_delete_table_row
# ---------------------------------------------------------------------------

class TestDeleteTableRow:
    def _table_page(self):
        adf = make_adf([make_table([["A", "B"], ["C", "D"]])])
        return make_page_response(adf=adf)

    @respx.mock
    async def test_success_with_preview(self, tmp_cache):
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=self._table_page())
        )
        respx.put(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=_push_result())
        )
        result = await server.confluence_delete_table_row("12345", 0)
        text = result.content[0].text
        assert "Deleted row 0" in text

    @respx.mock
    async def test_row_oob(self, tmp_cache):
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=self._table_page())
        )
        result = await server.confluence_delete_table_row("12345", 10)
        assert "Row 10 out of range" in result.content[0].text

    @respx.mock
    async def test_no_tables(self, tmp_cache):
        page = make_page_response()
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=page)
        )
        result = await server.confluence_delete_table_row("12345", 0)
        assert "No tables found" in result.content[0].text


# ---------------------------------------------------------------------------
# confluence_add_comment
# ---------------------------------------------------------------------------

class TestAddComment:
    @respx.mock
    async def test_adf_body_wrapping(self):
        respx.post(f"{BASE}/api/v2/footer-comments").mock(
            return_value=httpx.Response(200, json={"id": "c1"})
        )
        result = await server.confluence_add_comment("1", "Great work!")
        body = json.loads(respx.calls[0].request.content)
        adf_value = json.loads(body["body"]["value"])
        assert adf_value["type"] == "doc"
        assert adf_value["content"][0]["content"][0]["text"] == "Great work!"
        assert "c1" in result.content[0].text

    @respx.mock
    async def test_parent_comment_id(self):
        respx.post(f"{BASE}/api/v2/footer-comments").mock(
            return_value=httpx.Response(200, json={"id": "c2"})
        )
        await server.confluence_add_comment("1", "Reply", parent_comment_id="c1")
        body = json.loads(respx.calls[0].request.content)
        assert body["parentCommentId"] == "c1"


# ---------------------------------------------------------------------------
# confluence_add_link
# ---------------------------------------------------------------------------

class TestAddLink:
    @respx.mock
    async def test_append_paragraph(self, tmp_cache):
        page = make_page_response()
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=page)
        )
        respx.put(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=_push_result())
        )
        result = await server.confluence_add_link("12345", "Example", "https://example.com")
        assert "Added link" in result.content[0].text

    @respx.mock
    async def test_inline_after_text(self, tmp_cache):
        adf = make_adf([make_paragraph("See here for details.")])
        page = make_page_response(adf=adf)
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=page)
        )
        respx.put(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=_push_result())
        )
        result = await server.confluence_add_link("12345", "Link", "https://x.com", after_text="See here")
        assert "Added link" in result.content[0].text

    @respx.mock
    async def test_after_text_not_found(self, tmp_cache):
        page = make_page_response()
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=page)
        )
        result = await server.confluence_add_link("12345", "Link", "https://x.com", after_text="MISSING")
        assert "not found" in result.content[0].text


# ---------------------------------------------------------------------------
# confluence_upload_attachment
# ---------------------------------------------------------------------------

class TestUploadAttachment:
    async def test_file_not_found(self):
        result = await server.confluence_upload_attachment("1", "/nonexistent/file.txt")
        assert "File not found" in result.content[0].text

    @respx.mock
    async def test_multipart_and_headers(self, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")
        respx.post(f"{BASE}/rest/api/content/1/child/attachment").mock(
            return_value=httpx.Response(200, json={"results": [{"id": "a1", "title": "test.txt"}]})
        )
        result = await server.confluence_upload_attachment("1", str(test_file))
        assert "Uploaded" in result.content[0].text
        req = respx.calls[0].request
        assert req.headers.get("X-Atlassian-Token") == "nocheck"

    @respx.mock
    async def test_with_comment(self, tmp_path):
        test_file = tmp_path / "doc.pdf"
        test_file.write_bytes(b"PDF content")
        respx.post(f"{BASE}/rest/api/content/1/child/attachment").mock(
            return_value=httpx.Response(200, json={"results": [{"id": "a2", "title": "doc.pdf"}]})
        )
        result = await server.confluence_upload_attachment("1", str(test_file), comment="Updated doc")
        assert "doc.pdf" in result.content[0].text


# ---------------------------------------------------------------------------
# confluence_set_restrictions
# ---------------------------------------------------------------------------

class TestSetRestrictions:
    async def test_invalid_op(self):
        result = await server.confluence_set_restrictions("1", "delete")
        assert "Invalid operation" in result.content[0].text

    @respx.mock
    async def test_users_and_groups(self):
        respx.put(f"{BASE}/rest/api/content/1/restriction").mock(
            return_value=httpx.Response(200, json={})
        )
        result = await server.confluence_set_restrictions("1", "read", users=["u1"], groups=["g1"])
        text = result.content[0].text
        assert "1 user(s)" in text
        assert "1 group(s)" in text

    @respx.mock
    async def test_empty_clears(self):
        respx.put(f"{BASE}/rest/api/content/1/restriction").mock(
            return_value=httpx.Response(200, json={})
        )
        result = await server.confluence_set_restrictions("1", "update", users=[], groups=[])
        assert "Cleared" in result.content[0].text


# ---------------------------------------------------------------------------
# confluence_watch_page
# ---------------------------------------------------------------------------

class TestWatchPage:
    @respx.mock
    async def test_post_for_watch(self):
        respx.post(f"{BASE}/rest/api/user/watch/content/1").mock(
            return_value=httpx.Response(200)
        )
        result = await server.confluence_watch_page("1", watch=True)
        assert "Watching" in result.content[0].text

    @respx.mock
    async def test_delete_for_unwatch(self):
        respx.delete(f"{BASE}/rest/api/user/watch/content/1").mock(
            return_value=httpx.Response(200)
        )
        result = await server.confluence_watch_page("1", watch=False)
        assert "Unwatched" in result.content[0].text


# ---------------------------------------------------------------------------
# confluence_add_labels
# ---------------------------------------------------------------------------

class TestAddLabels:
    @respx.mock
    async def test_post_with_label_payload(self):
        respx.post(f"{BASE}/rest/api/content/1/label").mock(
            return_value=httpx.Response(200, json={"results": [{"name": "important"}, {"name": "v2"}]})
        )
        result = await server.confluence_add_labels("1", ["important", "v2"])
        text = result.content[0].text
        assert "Added 2 label(s)" in text
        body = json.loads(respx.calls[0].request.content)
        assert len(body) == 2
        assert body[0]["prefix"] == "global"
        assert body[0]["name"] == "important"


# ---------------------------------------------------------------------------
# confluence_remove_label
# ---------------------------------------------------------------------------

class TestRemoveLabel:
    @respx.mock
    async def test_success(self):
        respx.delete(f"{BASE}/rest/api/content/1/label/old").mock(
            return_value=httpx.Response(204)
        )
        result = await server.confluence_remove_label("1", "old")
        assert 'Removed label "old"' in result.content[0].text

    @respx.mock
    async def test_404_graceful(self):
        respx.delete(f"{BASE}/rest/api/content/1/label/missing").mock(
            return_value=httpx.Response(404)
        )
        result = await server.confluence_remove_label("1", "missing")
        assert "was not on this page" in result.content[0].text


# ---------------------------------------------------------------------------
# confluence_archive_page
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# confluence_download_attachment
# ---------------------------------------------------------------------------

class TestDownloadAttachment:
    @respx.mock
    async def test_download_success(self, tmp_path):
        respx.get(f"{BASE}/api/v2/pages/1/attachments").mock(
            return_value=httpx.Response(200, json={"results": [
                {"id": "a1", "title": "report.pdf", "mediaType": "application/pdf", "fileSize": 1024},
            ]})
        )
        respx.get(f"{BASE}/rest/api/content/a1/download").mock(
            return_value=httpx.Response(200, content=b"PDF bytes here")
        )
        save_path = str(tmp_path / "out" / "report.pdf")
        result = await server.confluence_download_attachment("1", "report.pdf", save_path)
        text = result.content[0].text
        assert "Downloaded" in text
        assert "report.pdf" in text
        from pathlib import Path
        assert Path(save_path).read_bytes() == b"PDF bytes here"

    @respx.mock
    async def test_attachment_not_found(self):
        respx.get(f"{BASE}/api/v2/pages/1/attachments").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        result = await server.confluence_download_attachment("1", "missing.txt", "/tmp/x")
        assert "not found" in result.content[0].text

    @respx.mock
    async def test_creates_parent_dirs(self, tmp_path):
        respx.get(f"{BASE}/api/v2/pages/1/attachments").mock(
            return_value=httpx.Response(200, json={"results": [
                {"id": "a1", "title": "f.txt"},
            ]})
        )
        respx.get(f"{BASE}/rest/api/content/a1/download").mock(
            return_value=httpx.Response(200, content=b"data")
        )
        save_path = str(tmp_path / "deep" / "nested" / "f.txt")
        await server.confluence_download_attachment("1", "f.txt", save_path)
        from pathlib import Path
        assert Path(save_path).exists()


# ---------------------------------------------------------------------------
# confluence_delete_attachment
# ---------------------------------------------------------------------------

class TestDeleteAttachment:
    @respx.mock
    async def test_preview_without_confirm(self):
        respx.get(f"{BASE}/api/v2/pages/1/attachments").mock(
            return_value=httpx.Response(200, json={"results": [
                {"id": "a1", "title": "old.pdf", "mediaType": "application/pdf", "fileSize": 2048},
            ]})
        )
        result = await server.confluence_delete_attachment("1", "old.pdf")
        text = result.content[0].text
        assert "DELETE PREVIEW" in text
        assert "old.pdf" in text
        assert "confirm=True" in text

    @respx.mock
    async def test_confirm_deletes(self):
        respx.get(f"{BASE}/api/v2/pages/1/attachments").mock(
            return_value=httpx.Response(200, json={"results": [
                {"id": "a1", "title": "old.pdf", "mediaType": "application/pdf", "fileSize": 2048},
            ]})
        )
        respx.delete(f"{BASE}/rest/api/content/a1").mock(
            return_value=httpx.Response(204)
        )
        result = await server.confluence_delete_attachment("1", "old.pdf", confirm=True)
        text = result.content[0].text
        assert "Deleted" in text
        assert "old.pdf" in text

    @respx.mock
    async def test_attachment_not_found(self):
        respx.get(f"{BASE}/api/v2/pages/1/attachments").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        result = await server.confluence_delete_attachment("1", "nope.txt", confirm=True)
        assert "not found" in result.content[0].text


# ---------------------------------------------------------------------------
# confluence_add_inline_comment
# ---------------------------------------------------------------------------

class TestAddInlineComment:
    @respx.mock
    async def test_success(self):
        respx.post(f"{BASE}/api/v2/inline-comments").mock(
            return_value=httpx.Response(200, json={"id": "ic1"})
        )
        result = await server.confluence_add_inline_comment("1", "Fix this", "some text")
        text = result.content[0].text
        assert "ic1" in text
        assert "some text" in text
        body = json.loads(respx.calls[0].request.content)
        assert body["inlineCommentProperties"]["textSelection"] == "some text"

    @respx.mock
    async def test_match_index(self):
        respx.post(f"{BASE}/api/v2/inline-comments").mock(
            return_value=httpx.Response(200, json={"id": "ic2"})
        )
        await server.confluence_add_inline_comment("1", "Note", "word", match_index=2)
        body = json.loads(respx.calls[0].request.content)
        assert body["inlineCommentProperties"]["textSelectionMatchIndex"] == 2
        assert body["inlineCommentProperties"]["textSelectionMatchCount"] == 3

    @respx.mock
    async def test_adf_wrapping(self):
        respx.post(f"{BASE}/api/v2/inline-comments").mock(
            return_value=httpx.Response(200, json={"id": "ic3"})
        )
        await server.confluence_add_inline_comment("1", "Comment text", "sel")
        body = json.loads(respx.calls[0].request.content)
        adf_value = json.loads(body["body"]["value"])
        assert adf_value["type"] == "doc"
        assert adf_value["content"][0]["content"][0]["text"] == "Comment text"


# ---------------------------------------------------------------------------
# confluence_set_page_property
# ---------------------------------------------------------------------------

class TestSetPageProperty:
    @respx.mock
    async def test_create_new(self):
        respx.get(f"{BASE}/api/v2/pages/1/properties").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        respx.post(f"{BASE}/api/v2/pages/1/properties").mock(
            return_value=httpx.Response(200, json={"key": "status", "value": "done", "version": {"number": 1}})
        )
        result = await server.confluence_set_page_property("1", "status", '"done"')
        text = result.content[0].text
        assert "Created" in text
        assert "status" in text

    @respx.mock
    async def test_update_existing(self):
        respx.get(f"{BASE}/api/v2/pages/1/properties").mock(
            return_value=httpx.Response(200, json={"results": [
                {"id": "p1", "key": "status", "value": "draft", "version": {"number": 2}},
            ]})
        )
        respx.put(f"{BASE}/api/v2/pages/1/properties/p1").mock(
            return_value=httpx.Response(200, json={"key": "status", "value": "done", "version": {"number": 3}})
        )
        result = await server.confluence_set_page_property("1", "status", '"done"')
        text = result.content[0].text
        assert "Updated" in text
        body = json.loads(respx.calls[-1].request.content)
        assert body["version"]["number"] == 3

    @respx.mock
    async def test_json_object_value(self):
        respx.get(f"{BASE}/api/v2/pages/1/properties").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        respx.post(f"{BASE}/api/v2/pages/1/properties").mock(
            return_value=httpx.Response(200, json={"key": "meta", "value": {"a": 1}, "version": {"number": 1}})
        )
        await server.confluence_set_page_property("1", "meta", '{"a": 1}')
        body = json.loads(respx.calls[-1].request.content)
        assert body["value"] == {"a": 1}

    @respx.mock
    async def test_plain_string_fallback(self):
        respx.get(f"{BASE}/api/v2/pages/1/properties").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        respx.post(f"{BASE}/api/v2/pages/1/properties").mock(
            return_value=httpx.Response(200, json={"key": "note", "value": "hello", "version": {"number": 1}})
        )
        await server.confluence_set_page_property("1", "note", "hello")
        body = json.loads(respx.calls[-1].request.content)
        assert body["value"] == "hello"


# ---------------------------------------------------------------------------
# confluence_copy_page
# ---------------------------------------------------------------------------

class TestCopyPage:
    @respx.mock
    async def test_copy_default_title(self):
        page = make_page_response(title="Original")
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=page)
        )
        respx.post(f"{BASE}/rest/api/content/12345/copy").mock(
            return_value=httpx.Response(200, json={"id": "99"})
        )
        result = await server.confluence_copy_page("12345")
        text = result.content[0].text
        assert "Copy of Original" in text
        assert "id=99" in text
        body = json.loads(respx.calls[-1].request.content)
        assert body["pageTitle"] == "Copy of Original"

    @respx.mock
    async def test_copy_custom_title(self):
        page = make_page_response(title="Original")
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=page)
        )
        respx.post(f"{BASE}/rest/api/content/12345/copy").mock(
            return_value=httpx.Response(200, json={"id": "100"})
        )
        result = await server.confluence_copy_page("12345", title="My Copy")
        body = json.loads(respx.calls[-1].request.content)
        assert body["pageTitle"] == "My Copy"

    @respx.mock
    async def test_copy_to_destination(self):
        page = make_page_response()
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=page)
        )
        respx.post(f"{BASE}/rest/api/content/12345/copy").mock(
            return_value=httpx.Response(200, json={"id": "101"})
        )
        await server.confluence_copy_page("12345", destination_parent_id="50")
        body = json.loads(respx.calls[-1].request.content)
        assert body["destination"] == {"type": "parent_page", "value": "50"}

    @respx.mock
    async def test_copy_options(self):
        page = make_page_response()
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=page)
        )
        respx.post(f"{BASE}/rest/api/content/12345/copy").mock(
            return_value=httpx.Response(200, json={"id": "102"})
        )
        await server.confluence_copy_page("12345", copy_labels=False, copy_attachments=False)
        body = json.loads(respx.calls[-1].request.content)
        assert body["copyLabels"] is False
        assert body["copyAttachments"] is False


# ---------------------------------------------------------------------------
# confluence_archive_page
# ---------------------------------------------------------------------------

class TestArchivePage:
    @respx.mock
    async def test_preview_without_confirm(self):
        page = make_page_response()
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=page)
        )
        result = await server.confluence_archive_page("12345")
        text = result.content[0].text
        assert "ARCHIVE PREVIEW" in text
        assert "Test Page" in text
        assert "id=12345" in text
        assert "confirm=True" in text
        # No PUT should have been made
        put_calls = [c for c in respx.calls if c.request.method == "PUT"]
        assert len(put_calls) == 0

    @respx.mock
    async def test_confirm_archives(self):
        page = make_page_response()
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=page)
        )
        respx.put(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=_push_result())
        )
        result = await server.confluence_archive_page("12345", confirm=True)
        text = result.content[0].text
        assert "Archived" in text
        assert "Test Page" in text
        # Verify the PUT payload has status=archived
        put_calls = [c for c in respx.calls if c.request.method == "PUT"]
        assert len(put_calls) == 1
        body = json.loads(put_calls[0].request.content)
        assert body["status"] == "archived"

    @respx.mock
    async def test_preview_shows_space(self):
        page = make_page_response(space_id="TEAM")
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=page)
        )
        result = await server.confluence_archive_page("12345")
        assert "TEAM" in result.content[0].text

    @respx.mock
    async def test_http_error_on_confirm(self):
        page = make_page_response()
        respx.get(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(200, json=page)
        )
        respx.put(f"{BASE}/api/v2/pages/12345").mock(
            return_value=httpx.Response(403)
        )
        result = await server.confluence_archive_page("12345", confirm=True)
        assert "Permission denied" in result.content[0].text


# ---------------------------------------------------------------------------
# confluence_move_page
# ---------------------------------------------------------------------------

class TestMovePage:
    @respx.mock
    async def test_preview_without_confirm(self):
        src = make_page_response(page_id="10", title="Source Page", space_id="SP1")
        tgt = make_page_response(page_id="20", title="Target Parent", space_id="SP1")
        respx.get(f"{BASE}/api/v2/pages/10").mock(
            return_value=httpx.Response(200, json=src)
        )
        respx.get(f"{BASE}/api/v2/pages/20").mock(
            return_value=httpx.Response(200, json=tgt)
        )
        result = await server.confluence_move_page("10", "20")
        text = result.content[0].text
        assert "MOVE PREVIEW" in text
        assert "Source Page" in text
        assert "Target Parent" in text
        assert "confirm=True" in text
        # No PUT should have been made
        put_calls = [c for c in respx.calls if c.request.method == "PUT"]
        assert len(put_calls) == 0

    @respx.mock
    async def test_cross_space_warning(self):
        src = make_page_response(page_id="10", title="Src", space_id="SP1")
        tgt = make_page_response(page_id="20", title="Tgt", space_id="SP2")
        respx.get(f"{BASE}/api/v2/pages/10").mock(
            return_value=httpx.Response(200, json=src)
        )
        respx.get(f"{BASE}/api/v2/pages/20").mock(
            return_value=httpx.Response(200, json=tgt)
        )
        result = await server.confluence_move_page("10", "20")
        assert "CROSS-SPACE" in result.content[0].text

    @respx.mock
    async def test_confirm_moves(self):
        src = make_page_response(page_id="10", title="Source", space_id="SP1")
        tgt = make_page_response(page_id="20", title="Target", space_id="SP1")
        respx.get(f"{BASE}/api/v2/pages/10").mock(
            return_value=httpx.Response(200, json=src)
        )
        respx.get(f"{BASE}/api/v2/pages/20").mock(
            return_value=httpx.Response(200, json=tgt)
        )
        # v2 PUT for push_page_update
        respx.put(f"{BASE}/api/v2/pages/10").mock(
            return_value=httpx.Response(200, json={"id": "10", "title": "Source", "version": {"number": 2}})
        )
        # v1 PUT for ancestor update
        respx.put(f"{BASE}/rest/api/content/10").mock(
            return_value=httpx.Response(200, json={})
        )
        result = await server.confluence_move_page("10", "20", confirm=True)
        text = result.content[0].text
        assert "Moved" in text
        assert "Source" in text
        assert "Target" in text
        # Verify v1 PUT sets ancestors
        v1_puts = [c for c in respx.calls if "rest/api/content/10" in str(c.request.url) and c.request.method == "PUT"]
        assert len(v1_puts) == 1
        body = json.loads(v1_puts[0].request.content)
        assert body["ancestors"] == [{"id": "20"}]

    @respx.mock
    async def test_cross_space_confirm_message(self):
        src = make_page_response(page_id="10", title="Src", space_id="SP1")
        tgt = make_page_response(page_id="20", title="Tgt", space_id="SP2")
        respx.get(f"{BASE}/api/v2/pages/10").mock(
            return_value=httpx.Response(200, json=src)
        )
        respx.get(f"{BASE}/api/v2/pages/20").mock(
            return_value=httpx.Response(200, json=tgt)
        )
        respx.put(f"{BASE}/api/v2/pages/10").mock(
            return_value=httpx.Response(200, json={"id": "10", "title": "Src", "version": {"number": 2}})
        )
        respx.put(f"{BASE}/rest/api/content/10").mock(
            return_value=httpx.Response(200, json={})
        )
        result = await server.confluence_move_page("10", "20", confirm=True)
        text = result.content[0].text
        assert "cross-space" in text
        assert "SP1" in text
        assert "SP2" in text
