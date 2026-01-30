import json
import os
import re
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("confluence-adf")

CONFLUENCE_URL = os.environ["CONFLUENCE_URL"]
CONFLUENCE_USERNAME = os.environ["CONFLUENCE_USERNAME"]
CONFLUENCE_API_TOKEN = os.environ["CONFLUENCE_API_TOKEN"]
CACHE_DIR = Path(os.environ.get("CACHE_DIR", ".cache/confluence"))

BASE_URL = CONFLUENCE_URL.rstrip("/")


def _auth() -> httpx.BasicAuth:
    return httpx.BasicAuth(CONFLUENCE_USERNAME, CONFLUENCE_API_TOKEN)


def _cache_path(page_id: str) -> Path:
    return CACHE_DIR / f"{page_id}.json"


def _read_cache(page_id: str) -> dict:
    path = _cache_path(page_id)
    if not path.exists():
        raise FileNotFoundError(f"No cached page for {page_id}. Call confluence_get_page first.")
    return json.loads(path.read_text())


def _write_cache(page_id: str, data: dict) -> str:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(page_id)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return str(path.resolve())


async def _resolve_page_id(client: httpx.AsyncClient, page_id_or_url: str) -> str:
    """Resolve a page ID from a numeric ID or Confluence URL."""
    # Already a numeric ID
    if page_id_or_url.isdigit():
        return page_id_or_url

    # Full URL with /pages/{id}/
    m = re.search(r"/pages/(\d+)", page_id_or_url)
    if m:
        return m.group(1)

    # Tiny URL like /wiki/x/BwD5O or full URL with /wiki/x/
    if "/x/" in page_id_or_url or "tinyurl" in page_id_or_url:
        resp = await client.get(page_id_or_url, auth=_auth(), follow_redirects=True)
        resp.raise_for_status()
        m = re.search(r"/pages/(\d+)", str(resp.url))
        if m:
            return m.group(1)

    raise ValueError(f"Could not resolve page ID from: {page_id_or_url}")


async def _get_page_raw(client: httpx.AsyncClient, page_id: str) -> dict:
    """Fetch a page from the v2 API with ADF body."""
    resp = await client.get(
        f"{BASE_URL}/api/v2/pages/{page_id}",
        params={"body-format": "atlas_doc_format"},
        auth=_auth(),
    )
    resp.raise_for_status()
    return resp.json()


@mcp.tool()
async def confluence_get_page(page_id: str) -> str:
    """Fetch a Confluence page and cache it locally for editing.

    Returns the page metadata and the local cache file path. Edit the cached file
    directly, then call confluence_push_page to publish your changes.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        page_id = await _resolve_page_id(client, page_id)
        data = await _get_page_raw(client, page_id)

    adf_value = data.get("body", {}).get("atlas_doc_format", {}).get("value", "{}")
    adf = json.loads(adf_value)

    page_data = {
        "id": data["id"],
        "title": data["title"],
        "version": data["version"]["number"],
        "spaceId": data.get("spaceId"),
        "adf": adf,
    }

    cache_file = _write_cache(page_id, page_data)

    return json.dumps(
        {
            "id": data["id"],
            "title": data["title"],
            "version": data["version"]["number"],
            "spaceId": data.get("spaceId"),
            "cache_file": cache_file,
        },
        indent=2,
    )


@mcp.tool()
async def confluence_edit_page(
    page_id: str,
    find: str,
    replace: str,
    replace_all: bool = True,
) -> str:
    """Find and replace text in a cached Confluence page.

    Operates on the local cache file. Call confluence_get_page first to cache the page,
    then use this to make edits, then confluence_push_page to publish.

    Args:
        page_id: The page ID to edit.
        find: The text to find in the page content.
        replace: The text to replace it with.
        replace_all: If true, replace all occurrences. If false, replace only the first.
    """
    cached = _read_cache(page_id)

    count = 0
    found = False

    def _replace_text(node):
        nonlocal count, found
        if isinstance(node, dict):
            if node.get("type") == "text" and "text" in node:
                if find in node["text"]:
                    found = True
                    if replace_all:
                        count += node["text"].count(find)
                        node["text"] = node["text"].replace(find, replace)
                    elif not found or count == 0:
                        count = 1
                        node["text"] = node["text"].replace(find, replace, 1)
            for v in node.values():
                _replace_text(v)
        elif isinstance(node, list):
            for item in node:
                _replace_text(item)

    _replace_text(cached["adf"])

    if not found:
        return json.dumps({"error": f"Text not found: {find}"}, indent=2)

    cache_file = _write_cache(page_id, cached)

    return json.dumps(
        {
            "status": "edited",
            "replacements": count if replace_all else 1,
            "cache_file": cache_file,
        },
        indent=2,
    )


@mcp.tool()
async def confluence_push_page(
    page_id: str,
    version_message: str = "",
) -> str:
    """Push the cached page to Confluence.

    Reads title and ADF body from the local cache file, fetches the latest version
    number from Confluence to avoid conflicts, then publishes.

    Call confluence_get_page first, edit the cache file, then call this.

    Args:
        page_id: The page ID to push.
        version_message: Optional message describing the change.
    """
    cached = _read_cache(page_id)
    page_id = cached["id"]

    async with httpx.AsyncClient(timeout=30.0) as client:
        payload = {
            "id": page_id,
            "title": cached["title"],
            "status": "current",
            "version": {
                "number": cached["version"] + 1,
                "message": version_message or "Updated via MCP",
            },
            "body": {
                "representation": "atlas_doc_format",
                "value": json.dumps(cached["adf"]),
            },
        }

        resp = await client.put(
            f"{BASE_URL}/api/v2/pages/{page_id}",
            json=payload,
            auth=_auth(),
        )

        # Version conflict — refetch current version and retry once
        if resp.status_code == 409:
            current = await _get_page_raw(client, page_id)
            payload["version"]["number"] = current["version"]["number"] + 1
            resp = await client.put(
                f"{BASE_URL}/api/v2/pages/{page_id}",
                json=payload,
                auth=_auth(),
            )

        resp.raise_for_status()
        result = resp.json()

    # Update cache with new version
    cached["version"] = result["version"]["number"]
    _write_cache(page_id, cached)

    return json.dumps(
        {
            "id": result["id"],
            "title": result["title"],
            "version": result["version"]["number"],
            "status": "pushed",
        },
        indent=2,
    )


@mcp.tool()
async def confluence_create_page(
    space_id: str,
    title: str,
    adf_body: str,
    parent_id: str = "",
) -> str:
    """Create a new Confluence page with ADF content.

    Args:
        space_id: The space ID to create the page in.
        title: The page title.
        adf_body: The full ADF document as a JSON string, e.g. {"type": "doc", "version": 1, "content": [...]}.
        parent_id: Optional parent page ID to nest under.
    """
    payload = {
        "spaceId": space_id,
        "title": title,
        "status": "current",
        "body": {
            "representation": "atlas_doc_format",
            "value": adf_body,
        },
    }
    if parent_id:
        payload["parentId"] = parent_id

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{BASE_URL}/api/v2/pages",
            json=payload,
            auth=_auth(),
        )
        resp.raise_for_status()
        result = resp.json()

    return json.dumps(
        {
            "id": result["id"],
            "title": result["title"],
            "version": result["version"]["number"],
            "status": "created",
        },
        indent=2,
    )


if __name__ == "__main__":
    mcp.run()
