"""Tests for pure (non-async, no HTTP) functions in server.py."""

import json

import pytest

import server
from tests.factories import (
    make_adf,
    make_mention,
    make_paragraph,
    make_table,
    make_task_list,
)


# ---------------------------------------------------------------------------
# _text
# ---------------------------------------------------------------------------

class TestText:
    def test_normal_string(self):
        result = server._text("hello")
        assert result.content[0].text == "hello"

    def test_empty_string(self):
        result = server._text("")
        assert result.content[0].text == ""


# ---------------------------------------------------------------------------
# _parse_adf
# ---------------------------------------------------------------------------

class TestParseAdf:
    def test_normal(self):
        adf = make_adf([make_paragraph("hi")])
        data = {"body": {"atlas_doc_format": {"value": json.dumps(adf)}}}
        assert server._parse_adf(data) == adf

    def test_missing_body(self):
        assert server._parse_adf({}) == {}

    def test_missing_atlas_doc_format(self):
        assert server._parse_adf({"body": {}}) == {}

    def test_empty_value(self):
        data = {"body": {"atlas_doc_format": {"value": "{}"}}}
        assert server._parse_adf(data) == {}


# ---------------------------------------------------------------------------
# _cache_path / _read_cache / _write_cache
# ---------------------------------------------------------------------------

class TestCachePath:
    def test_returns_correct_format(self, tmp_cache):
        path = server._cache_path("12345")
        assert path.name == "12345.json"
        assert path.parent == tmp_cache

    def test_different_ids(self, tmp_cache):
        p1 = server._cache_path("111")
        p2 = server._cache_path("222")
        assert p1 != p2


class TestReadWriteCache:
    def test_round_trip(self, tmp_cache):
        data = {"id": "1", "title": "T", "adf": {}}
        server._write_cache("1", data)
        assert server._read_cache("1") == data

    def test_creates_dirs(self, tmp_cache):
        assert not tmp_cache.exists()
        server._write_cache("1", {"x": 1})
        assert tmp_cache.exists()

    def test_read_missing_raises(self, tmp_cache):
        with pytest.raises(FileNotFoundError, match="No cached page"):
            server._read_cache("nonexistent")

    def test_write_returns_path(self, tmp_cache):
        result = server._write_cache("1", {"x": 1})
        assert "1.json" in result

    def test_overwrite(self, tmp_cache):
        server._write_cache("1", {"v": 1})
        server._write_cache("1", {"v": 2})
        assert server._read_cache("1") == {"v": 2}


# ---------------------------------------------------------------------------
# _cache_after_push
# ---------------------------------------------------------------------------

class TestCacheAfterPush:
    def test_correct_structure(self, tmp_cache):
        result = {"id": "99", "title": "Pushed", "version": {"number": 5}}
        adf = make_adf([make_paragraph("body")])
        server._cache_after_push(result, adf, "SP1")
        cached = server._read_cache("99")
        assert cached["id"] == "99"
        assert cached["title"] == "Pushed"
        assert cached["version"] == 5
        assert cached["spaceId"] == "SP1"
        assert cached["adf"] == adf

    def test_missing_space_id_fallback(self, tmp_cache):
        result = {"id": "99", "title": "T", "version": {"number": 1}, "spaceId": "FROM_RESULT"}
        server._cache_after_push(result, {}, "")
        cached = server._read_cache("99")
        assert cached["spaceId"] == "FROM_RESULT"


# ---------------------------------------------------------------------------
# _extract_text_from_adf — comprehensive node type coverage
# ---------------------------------------------------------------------------

class TestExtractTextFromAdf:
    def test_text_node(self):
        assert server._extract_text_from_adf({"type": "text", "text": "hi"}) == "hi"

    def test_mention_node(self):
        node = make_mention("@Alice")
        assert server._extract_text_from_adf(node) == "@Alice"

    def test_emoji_node(self):
        node = {"type": "emoji", "attrs": {"shortName": ":smile:"}}
        assert server._extract_text_from_adf(node) == ":smile:"

    def test_inline_card(self):
        node = {"type": "inlineCard", "attrs": {"url": "https://example.com"}}
        assert server._extract_text_from_adf(node) == "https://example.com"

    def test_hard_break(self):
        assert server._extract_text_from_adf({"type": "hardBreak"}) == "\n"

    def test_status(self):
        node = {"type": "status", "attrs": {"text": "IN PROGRESS"}}
        assert server._extract_text_from_adf(node) == "[IN PROGRESS]"

    def test_paragraph(self):
        p = make_paragraph("Hello")
        result = server._extract_text_from_adf(p)
        assert result == "Hello\n"

    def test_heading(self):
        h = {"type": "heading", "attrs": {"level": 1}, "content": [{"type": "text", "text": "Title"}]}
        assert server._extract_text_from_adf(h) == "Title\n"

    def test_bullet_list(self):
        bl = {
            "type": "bulletList",
            "content": [
                {"type": "listItem", "content": [make_paragraph("A")]},
                {"type": "listItem", "content": [make_paragraph("B")]},
            ],
        }
        result = server._extract_text_from_adf(bl)
        assert "- A" in result
        assert "- B" in result

    def test_ordered_list(self):
        ol = {
            "type": "orderedList",
            "content": [
                {"type": "listItem", "content": [make_paragraph("First")]},
            ],
        }
        result = server._extract_text_from_adf(ol)
        assert "- First" in result

    def test_list_item_depth(self):
        li = {"type": "listItem", "content": [make_paragraph("Item")]}
        result = server._extract_text_from_adf(li, depth=2)
        assert result.startswith("    - ")  # 2*2 spaces + "- "

    def test_task_list(self):
        tl = make_task_list([("Do this", "TODO"), ("Done that", "DONE")])
        result = server._extract_text_from_adf(tl)
        assert "[ ] Do this" in result
        assert "[x] Done that" in result

    def test_task_item_todo(self):
        ti = {
            "type": "taskItem",
            "attrs": {"state": "TODO"},
            "content": [make_paragraph("Task")],
        }
        result = server._extract_text_from_adf(ti)
        assert "[ ]" in result
        assert "Task" in result

    def test_task_item_done(self):
        ti = {
            "type": "taskItem",
            "attrs": {"state": "DONE"},
            "content": [make_paragraph("Task")],
        }
        assert "[x]" in server._extract_text_from_adf(ti)

    def test_table(self):
        table = make_table([["A", "B"], ["C", "D"]])
        result = server._extract_text_from_adf(table)
        assert "A\tB" in result
        assert "C\tD" in result

    def test_table_row_tab_separated(self):
        row = {
            "type": "tableRow",
            "content": [
                {"type": "tableCell", "content": [make_paragraph("X")]},
                {"type": "tableCell", "content": [make_paragraph("Y")]},
            ],
        }
        result = server._extract_text_from_adf(row)
        assert "X\tY" in result

    def test_code_block_with_language(self):
        cb = {"type": "codeBlock", "attrs": {"language": "python"}, "content": [{"type": "text", "text": "print(1)"}]}
        result = server._extract_text_from_adf(cb)
        assert "```python" in result
        assert "print(1)" in result

    def test_code_block_without_language(self):
        cb = {"type": "codeBlock", "content": [{"type": "text", "text": "code"}]}
        result = server._extract_text_from_adf(cb)
        assert result.startswith("```\n")

    def test_blockquote(self):
        bq = {"type": "blockquote", "content": [make_paragraph("quoted")]}
        result = server._extract_text_from_adf(bq)
        assert "> quoted" in result

    def test_rule(self):
        assert server._extract_text_from_adf({"type": "rule"}) == "---\n"

    def test_panel(self):
        panel = {"type": "panel", "attrs": {"panelType": "warning"}, "content": [make_paragraph("Alert")]}
        result = server._extract_text_from_adf(panel)
        assert "[warning]" in result
        assert "Alert" in result

    def test_expand_with_title(self):
        exp = {"type": "expand", "attrs": {"title": "Details"}, "content": [make_paragraph("Body")]}
        result = server._extract_text_from_adf(exp)
        assert "▸ Details" in result
        assert "Body" in result

    def test_expand_without_title(self):
        exp = {"type": "expand", "attrs": {}, "content": [make_paragraph("Body")]}
        result = server._extract_text_from_adf(exp)
        assert "Body" in result
        assert "▸" not in result

    def test_nested_expand(self):
        exp = {
            "type": "nestedExpand",
            "attrs": {"title": "Details"},
            "content": [make_paragraph("Inner")],
        }
        result = server._extract_text_from_adf(exp)
        assert "▸ Details" in result
        assert "Inner" in result

    def test_date(self):
        node = {"type": "date", "attrs": {"timestamp": "1738540800000"}}
        assert server._extract_text_from_adf(node) == "1738540800000"

    def test_media_with_alt(self):
        node = {"type": "media", "attrs": {"type": "file", "alt": "screenshot.png"}}
        assert server._extract_text_from_adf(node) == "screenshot.png"

    def test_media_without_alt(self):
        node = {"type": "media", "attrs": {"type": "file", "id": "abc-123"}}
        assert server._extract_text_from_adf(node) == "[media]"

    def test_media_inline(self):
        node = {"type": "mediaInline", "attrs": {"type": "file", "alt": "doc.pdf"}}
        assert server._extract_text_from_adf(node) == "doc.pdf"

    def test_media_inline_without_alt(self):
        node = {"type": "mediaInline", "attrs": {"type": "file", "id": "xyz"}}
        assert server._extract_text_from_adf(node) == "[media]"

    def test_media_single(self):
        node = {
            "type": "mediaSingle",
            "attrs": {"layout": "center"},
            "content": [{"type": "media", "attrs": {"type": "file", "alt": "img.png"}}],
        }
        result = server._extract_text_from_adf(node)
        assert "img.png" in result

    def test_media_group(self):
        node = {
            "type": "mediaGroup",
            "content": [
                {"type": "media", "attrs": {"type": "file", "alt": "a.png"}},
                {"type": "media", "attrs": {"type": "file", "alt": "b.png"}},
            ],
        }
        result = server._extract_text_from_adf(node)
        assert "a.png" in result
        assert "b.png" in result

    def test_multi_bodied_extension(self):
        node = {
            "type": "multiBodiedExtension",
            "content": [
                {
                    "type": "extensionFrame",
                    "content": [make_paragraph("Tab content")],
                }
            ],
        }
        result = server._extract_text_from_adf(node)
        assert "Tab content" in result

    def test_extension_frame(self):
        node = {
            "type": "extensionFrame",
            "content": [make_paragraph("Frame body")],
        }
        result = server._extract_text_from_adf(node)
        assert "Frame body" in result

    def test_list_input(self):
        nodes = [{"type": "text", "text": "A"}, {"type": "text", "text": "B"}]
        assert server._extract_text_from_adf(nodes) == "AB"

    def test_non_dict_input(self):
        assert server._extract_text_from_adf("not a dict") == ""
        assert server._extract_text_from_adf(42) == ""

    def test_empty_content(self):
        assert server._extract_text_from_adf({"type": "paragraph", "content": []}) == "\n"

    def test_nested_structure(self):
        adf = make_adf([
            make_paragraph("Intro"),
            {"type": "bulletList", "content": [
                {"type": "listItem", "content": [make_paragraph("Bullet")]},
            ]},
        ])
        result = server._extract_text_from_adf(adf)
        assert "Intro" in result
        assert "- Bullet" in result


# ---------------------------------------------------------------------------
# _get_table_nodes
# ---------------------------------------------------------------------------

class TestGetTableNodes:
    def test_zero_tables(self):
        adf = make_adf([make_paragraph("no tables")])
        assert server._get_table_nodes(adf) == []

    def test_one_table(self):
        table = make_table([["A"]])
        adf = make_adf([table])
        result = server._get_table_nodes(adf)
        assert len(result) == 1
        assert result[0]["type"] == "table"

    def test_multiple_tables(self):
        adf = make_adf([make_table([["A"]]), make_paragraph("sep"), make_table([["B"]])])
        assert len(server._get_table_nodes(adf)) == 2

    def test_nested_in_other_structures(self):
        table = make_table([["X"]])
        panel = {"type": "panel", "attrs": {"panelType": "info"}, "content": [table]}
        adf = make_adf([panel])
        assert len(server._get_table_nodes(adf)) == 1


# ---------------------------------------------------------------------------
# _build_table_cell
# ---------------------------------------------------------------------------

class TestBuildTableCell:
    def test_normal(self):
        cell = server._build_table_cell("Hello")
        assert cell["type"] == "tableCell"
        assert cell["content"][0]["content"][0]["text"] == "Hello"

    def test_empty_string(self):
        cell = server._build_table_cell("")
        assert cell["content"][0]["content"] == []

    def test_custom_cell_type(self):
        cell = server._build_table_cell("H", "tableHeader")
        assert cell["type"] == "tableHeader"


# ---------------------------------------------------------------------------
# _build_table_row
# ---------------------------------------------------------------------------

class TestBuildTableRow:
    def test_n_cells(self):
        row = server._build_table_row(["A", "B", "C"])
        assert row["type"] == "tableRow"
        assert len(row["content"]) == 3

    def test_cell_type_propagation(self):
        row = server._build_table_row(["H1", "H2"], cell_type="tableHeader")
        for cell in row["content"]:
            assert cell["type"] == "tableHeader"
