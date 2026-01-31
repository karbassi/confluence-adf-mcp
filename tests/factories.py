"""ADF factory helpers for building test data."""

import json


def make_adf(content: list[dict] | None = None) -> dict:
    """Build a minimal ADF document."""
    return {
        "type": "doc",
        "version": 1,
        "content": content or [],
    }


def make_paragraph(text: str, marks: list[dict] | None = None) -> dict:
    """Build an ADF paragraph with a single text node."""
    text_node = {"type": "text", "text": text}
    if marks:
        text_node["marks"] = marks
    return {
        "type": "paragraph",
        "content": [text_node],
    }


def make_table(rows: list[list[str]], has_header: bool = False) -> dict:
    """Build an ADF table from a list of rows (each row is a list of strings)."""
    table_rows = []
    for i, cells in enumerate(rows):
        cell_type = "tableHeader" if (has_header and i == 0) else "tableCell"
        table_rows.append({
            "type": "tableRow",
            "content": [
                {
                    "type": cell_type,
                    "content": [make_paragraph(c)],
                }
                for c in cells
            ],
        })
    return {"type": "table", "content": table_rows}


def make_task_list(items: list[tuple[str, str]]) -> dict:
    """Build an ADF taskList. items = [(text, state), ...]."""
    return {
        "type": "taskList",
        "content": [
            {
                "type": "taskItem",
                "attrs": {"localId": str(i), "state": state},
                "content": [make_paragraph(text)],
            }
            for i, (text, state) in enumerate(items)
        ],
    }


def make_mention(text: str, account_id: str = "abc123") -> dict:
    """Build an ADF mention inline node."""
    return {
        "type": "mention",
        "attrs": {"id": account_id, "text": text, "accessLevel": ""},
    }


def make_page_response(
    page_id: str = "12345",
    title: str = "Test Page",
    version: int = 1,
    space_id: str = "SPACE1",
    adf: dict | None = None,
) -> dict:
    """Build a mock Confluence v2 API page response."""
    adf = adf or make_adf([make_paragraph("Hello world")])
    return {
        "id": page_id,
        "title": title,
        "status": "current",
        "spaceId": space_id,
        "version": {"number": version, "message": "", "createdAt": "2025-01-01T00:00:00Z", "authorId": "user1"},
        "body": {
            "atlas_doc_format": {
                "value": json.dumps(adf),
            },
        },
    }
