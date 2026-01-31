import asyncio
import difflib
import functools
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

mcp = FastMCP("confluence-adf")


def _text(msg: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=msg)])

CONFLUENCE_URL = os.environ["CONFLUENCE_URL"]
CONFLUENCE_USERNAME = os.environ["CONFLUENCE_USERNAME"]
CONFLUENCE_API_TOKEN = os.environ["CONFLUENCE_API_TOKEN"]
CACHE_DIR = Path(os.environ.get("CACHE_DIR", ".cache/confluence"))

BASE_URL = CONFLUENCE_URL.rstrip("/")


class _RetryTransport(httpx.AsyncBaseTransport):
    """Async transport that retries on 429 (rate-limited) responses."""

    def __init__(self, max_retries: int = 2):
        self._transport = httpx.AsyncHTTPTransport()
        self._max_retries = max_retries

    async def handle_async_request(self, request):
        for attempt in range(self._max_retries + 1):
            resp = await self._transport.handle_async_request(request)
            status = getattr(resp, "status", None) or getattr(resp, "status_code", 0)
            if status == 429 and attempt < self._max_retries:
                headers = dict(resp.headers)
                raw = headers.get(b"retry-after") or headers.get("retry-after", "2")
                wait = min(int(raw), 10)
                await asyncio.sleep(wait)
                continue
            return resp
        return resp  # unreachable but satisfies type checkers


def _make_client(timeout: float = 30.0) -> httpx.AsyncClient:
    """Create an HTTP client with automatic 429 retry."""
    return httpx.AsyncClient(timeout=timeout, transport=_RetryTransport())


def _auth() -> httpx.BasicAuth:
    return httpx.BasicAuth(CONFLUENCE_USERNAME, CONFLUENCE_API_TOKEN)


def _friendly_error(e: httpx.HTTPStatusError) -> str:
    """Map an HTTP error to a human-readable message."""
    status = e.response.status_code
    url_path = str(e.request.url).replace(BASE_URL, "")
    body = e.response.text[:200].strip()

    if status == 401:
        msg = "Authentication failed — check CONFLUENCE_USERNAME and CONFLUENCE_API_TOKEN."
    elif status == 403:
        msg = "Permission denied — your account lacks access to this resource."
    elif status == 404:
        msg = "Not found — the page, space, or resource does not exist."
    elif status == 429:
        msg = "Rate limited — Confluence is throttling requests. Try again shortly."
    elif 500 <= status < 600:
        msg = f"Confluence server error ({status})."
    else:
        msg = f"HTTP {status} error."

    detail = f" (path: {url_path})" if url_path else ""
    if body:
        detail += f"\nResponse: {body}"
    return msg + detail


def _with_error_handling(func):
    """Decorator that catches HTTP and file errors, returning friendly messages."""
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except httpx.HTTPStatusError as e:
            return _text(_friendly_error(e))
        except FileNotFoundError as e:
            return _text(str(e))
    return wrapper


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


def _parse_adf(data: dict) -> dict:
    """Extract parsed ADF dict from a v2 API page response."""
    adf_value = data.get("body", {}).get("atlas_doc_format", {}).get("value", "{}")
    return json.loads(adf_value)


async def _push_page_update(
    client: httpx.AsyncClient,
    page_id: str,
    title: str,
    adf: dict,
    current_version: int,
    message: str = "Updated via MCP",
) -> dict:
    """Push a page update with 409 conflict retry."""
    payload = {
        "id": page_id,
        "title": title,
        "status": "current",
        "version": {"number": current_version + 1, "message": message},
        "body": {
            "representation": "atlas_doc_format",
            "value": json.dumps(adf),
        },
    }

    resp = await client.put(
        f"{BASE_URL}/api/v2/pages/{page_id}",
        json=payload,
        auth=_auth(),
    )

    if resp.status_code == 409:
        current = await _get_page_raw(client, page_id)
        payload["version"]["number"] = current["version"]["number"] + 1
        resp = await client.put(
            f"{BASE_URL}/api/v2/pages/{page_id}",
            json=payload,
            auth=_auth(),
        )

    resp.raise_for_status()
    return resp.json()


def _cache_after_push(result: dict, adf: dict, space_id: str = "") -> None:
    """Update local cache after a successful push."""
    page_data = {
        "id": result["id"],
        "title": result["title"],
        "version": result["version"]["number"],
        "spaceId": space_id or result.get("spaceId"),
        "adf": adf,
    }
    _write_cache(result["id"], page_data)


def _extract_text_from_adf(node: dict | list, depth: int = 0) -> str:
    """Recursively extract text from an ADF tree with basic formatting.

    Produces readable plaintext: newlines between paragraphs, bullet prefixes
    for list items, tab-separated table cells, etc.
    """
    if isinstance(node, list):
        return "".join(_extract_text_from_adf(item, depth) for item in node)

    if not isinstance(node, dict):
        return ""

    node_type = node.get("type", "")

    # Leaf text node
    if node_type == "text":
        return node.get("text", "")

    # Mention node
    if node_type == "mention":
        return node.get("attrs", {}).get("text", "")

    # Emoji
    if node_type == "emoji":
        return node.get("attrs", {}).get("shortName", "")

    # Inline card (link)
    if node_type == "inlineCard":
        return node.get("attrs", {}).get("url", "")

    # Hard break
    if node_type == "hardBreak":
        return "\n"

    # Status lozenge
    if node_type == "status":
        return f"[{node.get('attrs', {}).get('text', '')}]"

    content = node.get("content", [])
    inner = _extract_text_from_adf(content, depth)

    # Block-level formatting
    if node_type in ("paragraph", "heading"):
        return inner + "\n"

    if node_type == "bulletList":
        return inner

    if node_type == "orderedList":
        return inner

    if node_type == "listItem":
        prefix = "  " * depth + "- "
        # Indent inner content lines
        lines = inner.strip().split("\n")
        result = prefix + lines[0] + "\n"
        for line in lines[1:]:
            result += "  " * depth + "  " + line + "\n"
        return result

    if node_type == "taskList":
        return inner

    if node_type == "taskItem":
        state = node.get("attrs", {}).get("state", "TODO")
        checkbox = "[x]" if state == "DONE" else "[ ]"
        return f"  {checkbox} {inner.strip()}\n"

    if node_type == "table":
        return inner + "\n"

    if node_type == "tableRow":
        cells = content
        parts = [_extract_text_from_adf(c, depth).strip() for c in cells]
        return "\t".join(parts) + "\n"

    if node_type in ("tableCell", "tableHeader"):
        return inner

    if node_type == "codeBlock":
        lang = node.get("attrs", {}).get("language", "")
        header = f"```{lang}\n" if lang else "```\n"
        return header + inner + "```\n"

    if node_type == "blockquote":
        lines = inner.strip().split("\n")
        return "\n".join(f"> {line}" for line in lines) + "\n"

    if node_type == "rule":
        return "---\n"

    if node_type == "panel":
        panel_type = node.get("attrs", {}).get("panelType", "info")
        return f"[{panel_type}] {inner}"

    if node_type == "expand":
        title = node.get("attrs", {}).get("title", "")
        return f"▸ {title}\n{inner}" if title else inner

    # Default: just return inner content
    return inner


def _get_table_nodes(adf: dict) -> list[dict]:
    """Find all table nodes in the ADF tree."""
    tables = []

    def _walk(node):
        if isinstance(node, dict):
            if node.get("type") == "table":
                tables.append(node)
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(adf)
    return tables


def _build_table_cell(value: str, cell_type: str = "tableCell") -> dict:
    """Build a single ADF table cell with a text paragraph."""
    return {
        "type": cell_type,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": value}] if value else [],
            }
        ],
    }


def _build_table_row(values: list[str], cell_type: str = "tableCell") -> dict:
    """Build an ADF tableRow from a list of string values."""
    return {
        "type": "tableRow",
        "content": [_build_table_cell(v, cell_type) for v in values],
    }


def _extract_next_cursor(data: dict) -> str:
    """Extract pagination cursor from v2 API response _links.next."""
    next_url = data.get("_links", {}).get("next", "")
    if next_url:
        m = re.search(r"cursor=([^&]+)", next_url)
        if m:
            return m.group(1)
    return ""


async def _get_page_version_adf(client: httpx.AsyncClient, page_id: str, version: int) -> dict:
    """Fetch ADF for a specific historical version using the v1 API."""
    resp = await client.get(
        f"{BASE_URL}/rest/api/content/{page_id}",
        params={"version": version, "expand": "body.atlas_doc_format"},
        auth=_auth(),
    )
    resp.raise_for_status()
    data = resp.json()
    adf_value = data.get("body", {}).get("atlas_doc_format", {}).get("value", "{}")
    return json.loads(adf_value)


@mcp.tool()
@_with_error_handling
async def confluence_get_page(page_id: str) -> CallToolResult:
    """Fetch a Confluence page and cache it locally for editing.

    Returns the page metadata and the local cache file path. Edit the cached file
    directly, then call confluence_push_page to publish your changes.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
    """
    async with _make_client(timeout=30.0) as client:
        page_id = await _resolve_page_id(client, page_id)
        data = await _get_page_raw(client, page_id)

    adf = _parse_adf(data)

    page_data = {
        "id": data["id"],
        "title": data["title"],
        "version": data["version"]["number"],
        "spaceId": data.get("spaceId"),
        "adf": adf,
    }

    cache_file = _write_cache(page_id, page_data)

    return _text(f"Fetched \"{data['title']}\" (v{data['version']['number']}, id={data['id']}, space={data.get('spaceId')}). Cached at {cache_file}")


@mcp.tool()
@_with_error_handling
async def confluence_edit_page(
    page_id: str,
    find: str,
    replace: str,
    replace_all: bool = True,
) -> CallToolResult:
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
        return _text(f"Text not found: {find}")

    cache_file = _write_cache(page_id, cached)
    n = count if replace_all else 1

    return _text(f"Edited {n} replacement(s) in cache. File: {cache_file}")


@mcp.tool()
@_with_error_handling
async def confluence_push_page(
    page_id: str,
    version_message: str = "",
) -> CallToolResult:
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

    async with _make_client(timeout=30.0) as client:
        result = await _push_page_update(
            client, page_id, cached["title"], cached["adf"],
            cached["version"], version_message or "Updated via MCP",
        )

    _cache_after_push(result, cached["adf"], cached.get("spaceId"))

    return _text(f"Pushed \"{result['title']}\" to v{result['version']['number']}.")


@mcp.tool()
@_with_error_handling
async def confluence_find_replace(
    page_id: str,
    find: str,
    replace: str,
    replace_all: bool = True,
    version_message: str = "",
) -> CallToolResult:
    """Fetch a Confluence page, find and replace text, and push the result in one step.

    Combines get_page + edit_page + push_page into a single call. Only replaces
    within text content nodes — structural ADF elements are never modified.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
        find: The text to find in the page content.
        replace: The text to replace it with.
        replace_all: If true, replace all occurrences. If false, replace only the first.
        version_message: Optional message describing the change.
    """
    t0 = time.perf_counter()

    async with _make_client(timeout=30.0) as client:
        page_id = await _resolve_page_id(client, page_id)
        data = await _get_page_raw(client, page_id)
        adf = _parse_adf(data)

        # Edit
        count = 0

        def _replace_text(node):
            nonlocal count
            if isinstance(node, dict):
                if node.get("type") == "text" and "text" in node:
                    if find in node["text"]:
                        if replace_all:
                            count += node["text"].count(find)
                            node["text"] = node["text"].replace(find, replace)
                        elif count == 0:
                            count = 1
                            node["text"] = node["text"].replace(find, replace, 1)
                for v in node.values():
                    _replace_text(v)
            elif isinstance(node, list):
                for item in node:
                    _replace_text(item)

        _replace_text(adf)

        if count == 0:
            elapsed = (time.perf_counter() - t0) * 1000
            return _text(f"Text not found: \"{find}\" ({elapsed:.0f}ms)")

        result = await _push_page_update(
            client, page_id, data["title"], adf,
            data["version"]["number"],
            version_message or f"Replaced '{find}' with '{replace}'",
        )

    _cache_after_push(result, adf, data.get("spaceId"))

    elapsed = (time.perf_counter() - t0) * 1000

    return _text(f"Replaced {count} occurrence(s) of \"{find}\" with \"{replace}\" in \"{result['title']}\" (v{result['version']['number']}). {elapsed:.0f}ms")


@mcp.tool()
@_with_error_handling
async def confluence_create_page(
    space_id: str,
    title: str,
    adf_body: str,
    parent_id: str = "",
) -> CallToolResult:
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

    async with _make_client(timeout=30.0) as client:
        resp = await client.post(
            f"{BASE_URL}/api/v2/pages",
            json=payload,
            auth=_auth(),
        )
        resp.raise_for_status()
        result = resp.json()

    return _text(f"Created \"{result['title']}\" (v{result['version']['number']}, id={result['id']}).")


@mcp.tool()
@_with_error_handling
async def confluence_replace_mention(
    page_id: str,
    find_user: str,
    replace_user: str,
) -> CallToolResult:
    """Replace all @mentions of one user with another on a Confluence page.

    Fetches the page, searches for mention nodes matching find_user, looks up
    the replace_user's account ID, swaps the mentions, and pushes in one step.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
        find_user: Name to find (partial match on mention text, e.g. "Ali").
        replace_user: Name to replace with (searched in Confluence users, e.g. "Mark").
    """
    t0 = time.perf_counter()

    async with _make_client(timeout=30.0) as client:
        page_id = await _resolve_page_id(client, page_id)

        # Fetch page and search user in parallel
        async def _fetch_page():
            return await _get_page_raw(client, page_id)

        async def _search_user():
            resp = await client.get(
                f"{BASE_URL}/rest/api/search/user",
                params={"cql": f'user.fullname~"{replace_user}"'},
                auth=_auth(),
            )
            resp.raise_for_status()
            return resp.json().get("results", [])

        data, users = await asyncio.gather(_fetch_page(), _search_user())

        adf = _parse_adf(data)
        if not users:
            elapsed = (time.perf_counter() - t0) * 1000
            return _text(f"User not found: \"{replace_user}\" ({elapsed:.0f}ms)")

        if len(users) > 1:
            lines = [f"Multiple users match \"{replace_user}\". Please pick one by passing their exact display name:"]
            for u in users:
                info = u.get("user", {})
                lines.append(f"  - {info.get('displayName', '?')} ({info.get('accountId', '?')})")
            elapsed = (time.perf_counter() - t0) * 1000
            lines.append(f"({elapsed:.0f}ms)")
            return _text("\n".join(lines))

        new_user = users[0].get("user", {})
        new_account_id = new_user.get("accountId", "")
        new_display = new_user.get("displayName", replace_user)

        # Walk ADF and replace mentions
        count = 0

        def _replace_mentions(node):
            nonlocal count
            if isinstance(node, dict):
                if (
                    node.get("type") == "mention"
                    and "attrs" in node
                    and find_user.lower() in node["attrs"].get("text", "").lower()
                ):
                    node["attrs"]["id"] = new_account_id
                    node["attrs"]["text"] = f"@{new_display}"
                    count += 1
                for v in node.values():
                    _replace_mentions(v)
            elif isinstance(node, list):
                for item in node:
                    _replace_mentions(item)

        _replace_mentions(adf)

        if count == 0:
            elapsed = (time.perf_counter() - t0) * 1000
            return _text(f"No mentions found matching \"{find_user}\" ({elapsed:.0f}ms)")

        result = await _push_page_update(
            client, page_id, data["title"], adf,
            data["version"]["number"],
            f"Replaced @{find_user} mentions with @{new_display}",
        )

    _cache_after_push(result, adf, data.get("spaceId"))

    elapsed = (time.perf_counter() - t0) * 1000

    return _text(f"Replaced {count} mention(s) of \"{find_user}\" with \"@{new_display}\" in \"{result['title']}\" (v{result['version']['number']}). {elapsed:.0f}ms")


@mcp.tool()
@_with_error_handling
async def confluence_search_pages(
    query: str,
    limit: int = 10,
    cursor: str = "",
) -> CallToolResult:
    """Search Confluence pages using CQL (Confluence Query Language).

    Returns page titles, IDs, and spaces matching the query. Supports CQL operators
    like AND, OR, ~, =, etc. Simple text is treated as a title/content search.

    Args:
        query: CQL query string, e.g. 'type=page AND title~"meeting notes"' or just "meeting notes".
        limit: Maximum number of results to return (default 10, max 50).
        cursor: Pagination cursor from a previous search result.
    """
    limit = min(limit, 50)
    # If the query doesn't contain CQL operators, wrap it as a text search
    cql = query if any(op in query for op in ("=", "~", " AND ", " OR ", " IN ")) else f'type=page AND (title~"{query}" OR text~"{query}")'

    params: dict = {"cql": cql, "limit": limit}
    if cursor:
        params["cursor"] = cursor

    async with _make_client(timeout=30.0) as client:
        resp = await client.get(
            f"{BASE_URL}/rest/api/search",
            params=params,
            auth=_auth(),
        )
        resp.raise_for_status()
        data = resp.json()

    results = data.get("results", [])
    if not results:
        return _text("No pages found.")

    lines = [f"Found {len(results)} result(s):"]
    for r in results:
        content = r.get("content", {})
        title = content.get("title", r.get("title", "?"))
        page_id = content.get("id", "?")
        space = r.get("resultGlobalContainer", {}).get("title", "?")
        excerpt = r.get("excerpt", "").strip()
        if excerpt:
            # Clean HTML tags from excerpt
            excerpt = re.sub(r"<[^>]+>", "", excerpt)[:120]
        line = f"  [{page_id}] \"{title}\" (space: {space})"
        if excerpt:
            line += f" — {excerpt}"
        lines.append(line)

    next_cursor = _extract_next_cursor(data)
    if next_cursor:
        lines.append(f"\nNext cursor: {next_cursor}")

    return _text("\n".join(lines))


@mcp.tool()
@_with_error_handling
async def confluence_list_pages(
    space_id: str,
    limit: int = 25,
    sort: str = "title",
    cursor: str = "",
) -> CallToolResult:
    """List pages in a Confluence space.

    Args:
        space_id: The numeric space ID.
        limit: Maximum number of pages to return (default 25, max 250).
        sort: Sort order — "title", "-title", "created-date", "-modified-date", etc.
        cursor: Pagination cursor from a previous result.
    """
    limit = min(limit, 250)
    params: dict = {"limit": limit, "sort": sort}
    if cursor:
        params["cursor"] = cursor

    async with _make_client(timeout=30.0) as client:
        resp = await client.get(
            f"{BASE_URL}/api/v2/spaces/{space_id}/pages",
            params=params,
            auth=_auth(),
        )
        resp.raise_for_status()
        data = resp.json()

    pages = data.get("results", [])
    if not pages:
        return _text("No pages found in this space.")

    lines = [f"{len(pages)} page(s) in space {space_id}:"]
    for p in pages:
        status = p.get("status", "")
        status_tag = f" [{status}]" if status and status != "current" else ""
        lines.append(f"  [{p['id']}] \"{p['title']}\"{status_tag}")

    next_cursor = _extract_next_cursor(data)
    if next_cursor:
        lines.append(f"\nNext cursor: {next_cursor}")

    return _text("\n".join(lines))


@mcp.tool()
@_with_error_handling
async def confluence_get_child_pages(
    page_id: str,
    limit: int = 25,
    cursor: str = "",
) -> CallToolResult:
    """Get child pages of a Confluence page.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
        limit: Maximum number of children to return (default 25, max 250).
        cursor: Pagination cursor from a previous result.
    """
    limit = min(limit, 250)
    params: dict = {"limit": limit}
    if cursor:
        params["cursor"] = cursor

    async with _make_client(timeout=30.0) as client:
        page_id = await _resolve_page_id(client, page_id)
        resp = await client.get(
            f"{BASE_URL}/api/v2/pages/{page_id}/children",
            params=params,
            auth=_auth(),
        )
        resp.raise_for_status()
        data = resp.json()

    children = data.get("results", [])
    if not children:
        return _text("No child pages found.")

    lines = [f"{len(children)} child page(s):"]
    for c in children:
        lines.append(f"  [{c['id']}] \"{c['title']}\"")

    next_cursor = _extract_next_cursor(data)
    if next_cursor:
        lines.append(f"\nNext cursor: {next_cursor}")

    return _text("\n".join(lines))


@mcp.tool()
@_with_error_handling
async def confluence_get_ancestors(
    page_id: str,
) -> CallToolResult:
    """Get the ancestor (parent) chain of a Confluence page.

    Returns the page hierarchy from the space root down to the immediate parent.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
    """
    async with _make_client(timeout=30.0) as client:
        page_id = await _resolve_page_id(client, page_id)
        resp = await client.get(
            f"{BASE_URL}/api/v2/pages/{page_id}/ancestors",
            auth=_auth(),
        )
        resp.raise_for_status()
        data = resp.json()

    ancestors = data.get("results", [])
    if not ancestors:
        return _text("No ancestors — this is a root-level page.")

    lines = [f"{len(ancestors)} ancestor(s) (root → parent):"]
    for i, a in enumerate(ancestors):
        indent = "  " * (i + 1)
        lines.append(f"{indent}[{a['id']}] \"{a['title']}\"")

    return _text("\n".join(lines))


@mcp.tool()
@_with_error_handling
async def confluence_get_labels(
    page_id: str,
) -> CallToolResult:
    """Get all labels on a Confluence page.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
    """
    async with _make_client(timeout=30.0) as client:
        page_id = await _resolve_page_id(client, page_id)
        resp = await client.get(
            f"{BASE_URL}/api/v2/pages/{page_id}/labels",
            auth=_auth(),
        )
        resp.raise_for_status()
        data = resp.json()

    labels = data.get("results", [])
    if not labels:
        return _text("No labels on this page.")

    names = [l.get("name", "?") for l in labels]
    return _text(f"{len(names)} label(s): {', '.join(names)}")


@mcp.tool()
@_with_error_handling
async def confluence_add_labels(
    page_id: str,
    labels: list[str],
) -> CallToolResult:
    """Add labels to a Confluence page.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
        labels: List of label names to add, e.g. ["important", "reviewed"].
    """
    async with _make_client(timeout=30.0) as client:
        page_id = await _resolve_page_id(client, page_id)
        payload = [{"prefix": "global", "name": name} for name in labels]
        resp = await client.post(
            f"{BASE_URL}/rest/api/content/{page_id}/label",
            json=payload,
            auth=_auth(),
        )
        resp.raise_for_status()
        result = resp.json()

    added = result.get("results", result) if isinstance(result, dict) else result
    count = len(added) if isinstance(added, list) else len(labels)
    return _text(f"Added {count} label(s): {', '.join(labels)}")


@mcp.tool()
@_with_error_handling
async def confluence_remove_label(
    page_id: str,
    label: str,
) -> CallToolResult:
    """Remove a label from a Confluence page.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
        label: The label name to remove.
    """
    async with _make_client(timeout=30.0) as client:
        page_id = await _resolve_page_id(client, page_id)
        resp = await client.delete(
            f"{BASE_URL}/rest/api/content/{page_id}/label/{label}",
            auth=_auth(),
        )
        # 404 means label wasn't there — not an error
        if resp.status_code == 404:
            return _text(f"Label \"{label}\" was not on this page.")
        resp.raise_for_status()

    return _text(f"Removed label \"{label}\".")


@mcp.tool()
@_with_error_handling
async def confluence_list_versions(
    page_id: str,
    limit: int = 10,
    cursor: str = "",
) -> CallToolResult:
    """List version history of a Confluence page.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
        limit: Maximum number of versions to return (default 10, max 50).
        cursor: Pagination cursor from a previous result.
    """
    limit = min(limit, 50)
    params: dict = {"limit": limit}
    if cursor:
        params["cursor"] = cursor

    async with _make_client(timeout=30.0) as client:
        page_id = await _resolve_page_id(client, page_id)
        resp = await client.get(
            f"{BASE_URL}/api/v2/pages/{page_id}/versions",
            params=params,
            auth=_auth(),
        )
        resp.raise_for_status()
        data = resp.json()

    versions = data.get("results", [])
    if not versions:
        return _text("No version history found.")

    lines = [f"{len(versions)} version(s):"]
    for v in versions:
        num = v.get("number", "?")
        msg = v.get("message", "")
        author = v.get("authorId", "?")
        created = v.get("createdAt", "?")
        line = f"  v{num} by {author} at {created}"
        if msg:
            line += f" — \"{msg}\""
        lines.append(line)

    next_cursor = _extract_next_cursor(data)
    if next_cursor:
        lines.append(f"\nNext cursor: {next_cursor}")

    return _text("\n".join(lines))


@mcp.tool()
@_with_error_handling
async def confluence_extract_text(
    page_id: str,
) -> CallToolResult:
    """Extract plain text content from a Confluence page.

    Fetches the page ADF and converts it to readable plaintext with basic
    formatting (paragraphs, bullet lists, tables, code blocks, etc.).

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
    """
    async with _make_client(timeout=30.0) as client:
        page_id = await _resolve_page_id(client, page_id)
        data = await _get_page_raw(client, page_id)

    adf = _parse_adf(data)
    text = _extract_text_from_adf(adf)

    title = data.get("title", "?")
    return _text(f"# {title}\n\n{text.strip()}")


@mcp.tool()
@_with_error_handling
async def confluence_update_task(
    page_id: str,
    task_text: str,
    state: str,
) -> CallToolResult:
    """Toggle a task (checkbox) item on a Confluence page.

    Finds a taskItem whose text contains task_text and sets its state.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
        task_text: Text substring to match the task item (e.g. "Review PR").
        state: New state — "DONE" or "TODO".
    """
    state = state.upper()
    if state not in ("DONE", "TODO"):
        return _text(f"Invalid state \"{state}\". Use \"DONE\" or \"TODO\".")

    async with _make_client(timeout=30.0) as client:
        page_id = await _resolve_page_id(client, page_id)
        data = await _get_page_raw(client, page_id)
        adf = _parse_adf(data)

        count = 0

        def _update_tasks(node):
            nonlocal count
            if isinstance(node, dict):
                if node.get("type") == "taskItem" and "attrs" in node:
                    text = _extract_text_from_adf(node).strip()
                    if task_text.lower() in text.lower():
                        node["attrs"]["state"] = state
                        count += 1
                for v in node.values():
                    _update_tasks(v)
            elif isinstance(node, list):
                for item in node:
                    _update_tasks(item)

        _update_tasks(adf)

        if count == 0:
            return _text(f"No task found matching \"{task_text}\".")

        result = await _push_page_update(
            client, page_id, data["title"], adf,
            data["version"]["number"],
            f"Set task '{task_text}' to {state}",
        )

    _cache_after_push(result, adf, data.get("spaceId"))

    return _text(f"Updated {count} task(s) matching \"{task_text}\" to {state} (v{result['version']['number']}).")


@mcp.tool()
@_with_error_handling
async def confluence_regex_replace(
    page_id: str,
    pattern: str,
    replacement: str,
    version_message: str = "",
) -> CallToolResult:
    """Find and replace text using a regex pattern on a Confluence page.

    Applies re.sub() to every text node in the ADF. Supports capture groups
    in the replacement string (e.g. r"\\1").

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
        pattern: Python regex pattern to match.
        replacement: Replacement string (supports backreferences like \\1).
        version_message: Optional message describing the change.
    """
    try:
        compiled = re.compile(pattern)
    except re.error as e:
        return _text(f"Invalid regex: {e}")

    async with _make_client(timeout=30.0) as client:
        page_id = await _resolve_page_id(client, page_id)
        data = await _get_page_raw(client, page_id)
        adf = _parse_adf(data)

        count = 0

        def _regex_replace(node):
            nonlocal count
            if isinstance(node, dict):
                if node.get("type") == "text" and "text" in node:
                    new_text, n = compiled.subn(replacement, node["text"])
                    if n > 0:
                        node["text"] = new_text
                        count += n
                for v in node.values():
                    _regex_replace(v)
            elif isinstance(node, list):
                for item in node:
                    _regex_replace(item)

        _regex_replace(adf)

        if count == 0:
            return _text(f"No matches for pattern: {pattern}")

        result = await _push_page_update(
            client, page_id, data["title"], adf,
            data["version"]["number"],
            version_message or f"Regex replace: {pattern}",
        )

    _cache_after_push(result, adf, data.get("spaceId"))

    return _text(f"Replaced {count} match(es) of /{pattern}/ in \"{result['title']}\" (v{result['version']['number']}).")


@mcp.tool()
@_with_error_handling
async def confluence_update_table_cell(
    page_id: str,
    row: int,
    col: int,
    value: str,
    table_index: int = 0,
) -> CallToolResult:
    """Update a single cell in a table on a Confluence page.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
        row: Zero-based row index.
        col: Zero-based column index.
        value: New text value for the cell.
        table_index: Which table on the page (0-based, default first table).
    """
    async with _make_client(timeout=30.0) as client:
        page_id = await _resolve_page_id(client, page_id)
        data = await _get_page_raw(client, page_id)
        adf = _parse_adf(data)

        tables = _get_table_nodes(adf)
        if not tables:
            return _text("No tables found on this page.")
        if table_index >= len(tables):
            return _text(f"Table index {table_index} out of range (page has {len(tables)} table(s)).")

        table = tables[table_index]
        rows = table.get("content", [])
        if row >= len(rows):
            return _text(f"Row {row} out of range (table has {len(rows)} row(s)).")

        cells = rows[row].get("content", [])
        if col >= len(cells):
            return _text(f"Column {col} out of range (row has {len(cells)} column(s)).")

        cell = cells[col]
        cell_type = cell.get("type", "tableCell")
        # Replace cell content with new value
        cell["content"] = [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": value}] if value else [],
            }
        ]

        result = await _push_page_update(
            client, page_id, data["title"], adf,
            data["version"]["number"],
            f"Updated table cell [{row},{col}]",
        )

    _cache_after_push(result, adf, data.get("spaceId"))

    return _text(f"Updated cell [{row},{col}] to \"{value}\" (v{result['version']['number']}).")


@mcp.tool()
@_with_error_handling
async def confluence_insert_table_row(
    page_id: str,
    row_index: int,
    values: list[str],
    table_index: int = 0,
) -> CallToolResult:
    """Insert a new row into a table on a Confluence page.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
        row_index: Position to insert at (0-based). Use -1 to append at the end.
        values: List of cell values for the new row.
        table_index: Which table on the page (0-based, default first table).
    """
    async with _make_client(timeout=30.0) as client:
        page_id = await _resolve_page_id(client, page_id)
        data = await _get_page_raw(client, page_id)
        adf = _parse_adf(data)

        tables = _get_table_nodes(adf)
        if not tables:
            return _text("No tables found on this page.")
        if table_index >= len(tables):
            return _text(f"Table index {table_index} out of range (page has {len(tables)} table(s)).")

        table = tables[table_index]
        rows = table.get("content", [])

        new_row = _build_table_row(values)

        if row_index == -1 or row_index >= len(rows):
            rows.append(new_row)
            pos = len(rows) - 1
        else:
            rows.insert(row_index, new_row)
            pos = row_index

        result = await _push_page_update(
            client, page_id, data["title"], adf,
            data["version"]["number"],
            f"Inserted table row at index {pos}",
        )

    _cache_after_push(result, adf, data.get("spaceId"))

    return _text(f"Inserted row at index {pos} with {len(values)} cell(s) (v{result['version']['number']}).")


@mcp.tool()
@_with_error_handling
async def confluence_delete_table_row(
    page_id: str,
    row_index: int,
    table_index: int = 0,
) -> CallToolResult:
    """Delete a row from a table on a Confluence page.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
        row_index: Zero-based row index to delete.
        table_index: Which table on the page (0-based, default first table).
    """
    async with _make_client(timeout=30.0) as client:
        page_id = await _resolve_page_id(client, page_id)
        data = await _get_page_raw(client, page_id)
        adf = _parse_adf(data)

        tables = _get_table_nodes(adf)
        if not tables:
            return _text("No tables found on this page.")
        if table_index >= len(tables):
            return _text(f"Table index {table_index} out of range (page has {len(tables)} table(s)).")

        table = tables[table_index]
        rows = table.get("content", [])
        if row_index >= len(rows):
            return _text(f"Row {row_index} out of range (table has {len(rows)} row(s)).")

        deleted = rows.pop(row_index)
        deleted_text = _extract_text_from_adf(deleted).strip()

        result = await _push_page_update(
            client, page_id, data["title"], adf,
            data["version"]["number"],
            f"Deleted table row {row_index}",
        )

    _cache_after_push(result, adf, data.get("spaceId"))

    return _text(f"Deleted row {row_index} (\"{deleted_text[:60]}\") (v{result['version']['number']}).")


@mcp.tool()
@_with_error_handling
async def confluence_add_comment(
    page_id: str,
    body: str,
    parent_comment_id: str = "",
) -> CallToolResult:
    """Add a footer comment to a Confluence page.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
        body: The comment text (plain text, converted to simple ADF paragraph).
        parent_comment_id: Optional parent comment ID for threaded replies.
    """
    adf_body = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": body}],
            }
        ],
    }

    async with _make_client(timeout=30.0) as client:
        page_id = await _resolve_page_id(client, page_id)
        payload = {
            "pageId": page_id,
            "body": {
                "representation": "atlas_doc_format",
                "value": json.dumps(adf_body),
            },
        }
        if parent_comment_id:
            payload["parentCommentId"] = parent_comment_id

        resp = await client.post(
            f"{BASE_URL}/api/v2/footer-comments",
            json=payload,
            auth=_auth(),
        )
        resp.raise_for_status()
        result = resp.json()

    comment_id = result.get("id", "?")
    return _text(f"Added comment (id={comment_id}) on page {page_id}.")


@mcp.tool()
@_with_error_handling
async def confluence_list_comments(
    page_id: str,
    limit: int = 25,
    cursor: str = "",
) -> CallToolResult:
    """List footer comments on a Confluence page.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
        limit: Maximum number of comments to return (default 25, max 100).
        cursor: Pagination cursor from a previous result.
    """
    limit = min(limit, 100)
    params: dict = {"limit": limit, "body-format": "atlas_doc_format"}
    if cursor:
        params["cursor"] = cursor

    async with _make_client(timeout=30.0) as client:
        page_id = await _resolve_page_id(client, page_id)
        resp = await client.get(
            f"{BASE_URL}/api/v2/pages/{page_id}/footer-comments",
            params=params,
            auth=_auth(),
        )
        resp.raise_for_status()
        data = resp.json()

    comments = data.get("results", [])
    if not comments:
        return _text("No comments on this page.")

    lines = [f"{len(comments)} comment(s):"]
    for c in comments:
        cid = c.get("id", "?")
        author = c.get("authorId", "?")
        created = c.get("createdAt", "?")
        body_adf = json.loads(c.get("body", {}).get("atlas_doc_format", {}).get("value", "{}"))
        text = _extract_text_from_adf(body_adf).strip()[:200]
        lines.append(f"  [{cid}] by {author} at {created}: {text}")

    next_cursor = _extract_next_cursor(data)
    if next_cursor:
        lines.append(f"\nNext cursor: {next_cursor}")

    return _text("\n".join(lines))


@mcp.tool()
@_with_error_handling
async def confluence_compare_versions(
    page_id: str,
    version_a: int,
    version_b: int,
) -> CallToolResult:
    """Compare two versions of a Confluence page as a unified text diff.

    Fetches the ADF for both versions, extracts plaintext, and produces a
    unified diff showing what changed.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
        version_a: The "before" version number.
        version_b: The "after" version number.
    """
    async with _make_client(timeout=30.0) as client:
        page_id = await _resolve_page_id(client, page_id)

        adf_a, adf_b = await asyncio.gather(
            _get_page_version_adf(client, page_id, version_a),
            _get_page_version_adf(client, page_id, version_b),
        )

    text_a = _extract_text_from_adf(adf_a).splitlines(keepends=True)
    text_b = _extract_text_from_adf(adf_b).splitlines(keepends=True)

    diff = list(difflib.unified_diff(
        text_a, text_b,
        fromfile=f"v{version_a}",
        tofile=f"v{version_b}",
    ))

    if not diff:
        return _text(f"No text differences between v{version_a} and v{version_b}.")

    return _text("".join(diff))


@mcp.tool()
@_with_error_handling
async def confluence_list_attachments(
    page_id: str,
    limit: int = 25,
    cursor: str = "",
) -> CallToolResult:
    """List attachments on a Confluence page.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
        limit: Maximum number of attachments to return (default 25, max 100).
        cursor: Pagination cursor from a previous result.
    """
    limit = min(limit, 100)
    params: dict = {"limit": limit}
    if cursor:
        params["cursor"] = cursor

    async with _make_client(timeout=30.0) as client:
        page_id = await _resolve_page_id(client, page_id)
        resp = await client.get(
            f"{BASE_URL}/api/v2/pages/{page_id}/attachments",
            params=params,
            auth=_auth(),
        )
        resp.raise_for_status()
        data = resp.json()

    attachments = data.get("results", [])
    if not attachments:
        return _text("No attachments on this page.")

    lines = [f"{len(attachments)} attachment(s):"]
    for a in attachments:
        aid = a.get("id", "?")
        title = a.get("title", "?")
        media_type = a.get("mediaType", "?")
        size = a.get("fileSize", 0)
        size_str = f"{size / 1024:.1f} KB" if size else "?"
        lines.append(f"  [{aid}] \"{title}\" ({media_type}, {size_str})")

    next_cursor = _extract_next_cursor(data)
    if next_cursor:
        lines.append(f"\nNext cursor: {next_cursor}")

    return _text("\n".join(lines))


@mcp.tool()
@_with_error_handling
async def confluence_get_contributors(
    page_id: str,
) -> CallToolResult:
    """Get unique contributors to a Confluence page from its version history.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
    """
    async with _make_client(timeout=30.0) as client:
        page_id = await _resolve_page_id(client, page_id)
        resp = await client.get(
            f"{BASE_URL}/api/v2/pages/{page_id}/versions",
            params={"limit": 50},
            auth=_auth(),
        )
        resp.raise_for_status()
        data = resp.json()

    versions = data.get("results", [])
    if not versions:
        return _text("No version history found.")

    # Extract unique authors preserving first-seen order
    seen = {}
    for v in versions:
        author_id = v.get("authorId", "")
        if author_id and author_id not in seen:
            seen[author_id] = v.get("number", "?")

    lines = [f"{len(seen)} contributor(s):"]
    for author_id, first_version in seen.items():
        lines.append(f"  {author_id} (first seen in v{first_version})")

    return _text("\n".join(lines))


@mcp.tool()
@_with_error_handling
async def confluence_add_link(
    page_id: str,
    link_text: str,
    url: str,
    after_text: str = "",
) -> CallToolResult:
    """Add a hyperlink to a Confluence page.

    If after_text is provided, the link is inserted right after the first occurrence
    of that text in a paragraph. Otherwise it's appended as a new paragraph at the
    end of the page.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
        link_text: The display text for the link.
        url: The URL to link to.
        after_text: Optional text to insert the link after (inline within a paragraph).
    """
    link_node = {
        "type": "text",
        "text": link_text,
        "marks": [{"type": "link", "attrs": {"href": url}}],
    }

    async with _make_client(timeout=30.0) as client:
        page_id = await _resolve_page_id(client, page_id)
        data = await _get_page_raw(client, page_id)
        adf = _parse_adf(data)

        inserted = False

        if after_text:
            # Walk ADF and split text node to insert link inline
            def _insert_after(node):
                nonlocal inserted
                if inserted:
                    return
                if isinstance(node, dict) and "content" in node:
                    content = node["content"]
                    if isinstance(content, list):
                        for i, child in enumerate(content):
                            if (
                                not inserted
                                and isinstance(child, dict)
                                and child.get("type") == "text"
                                and after_text in child.get("text", "")
                            ):
                                text = child["text"]
                                idx = text.index(after_text) + len(after_text)
                                before = text[:idx]
                                after = text[idx:]

                                new_nodes = []
                                if before:
                                    before_node = {"type": "text", "text": before}
                                    if "marks" in child:
                                        before_node["marks"] = child["marks"]
                                    new_nodes.append(before_node)
                                new_nodes.append({"type": "text", "text": " "})
                                new_nodes.append(link_node)
                                if after:
                                    after_node = {"type": "text", "text": after}
                                    if "marks" in child:
                                        after_node["marks"] = child["marks"]
                                    new_nodes.append(after_node)

                                content[i:i+1] = new_nodes
                                inserted = True
                                return
                        for child in content:
                            _insert_after(child)
                elif isinstance(node, dict):
                    for v in node.values():
                        if isinstance(v, (dict, list)):
                            _insert_after(v)
                elif isinstance(node, list):
                    for item in node:
                        _insert_after(item)

            _insert_after(adf)

            if not inserted:
                return _text(f"Text \"{after_text}\" not found on page.")
        else:
            # Append as new paragraph
            link_paragraph = {
                "type": "paragraph",
                "content": [link_node],
            }
            adf.setdefault("content", []).append(link_paragraph)
            inserted = True

        result = await _push_page_update(
            client, page_id, data["title"], adf,
            data["version"]["number"],
            f"Added link: {link_text}",
        )

    _cache_after_push(result, adf, data.get("spaceId"))

    return _text(f"Added link \"{link_text}\" → {url} (v{result['version']['number']}).")


@mcp.tool()
@_with_error_handling
async def confluence_upload_attachment(
    page_id: str,
    file_path: str,
    comment: str = "",
) -> CallToolResult:
    """Upload a file as an attachment to a Confluence page.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
        file_path: Local file path to upload.
        comment: Optional comment for the attachment.
    """
    path = Path(file_path)
    if not path.exists():
        return _text(f"File not found: {file_path}")

    async with _make_client(timeout=60.0) as client:
        page_id = await _resolve_page_id(client, page_id)

        files = {"file": (path.name, path.read_bytes())}
        data = {}
        if comment:
            data["comment"] = comment

        resp = await client.post(
            f"{BASE_URL}/rest/api/content/{page_id}/child/attachment",
            files=files,
            data=data,
            auth=_auth(),
            headers={"X-Atlassian-Token": "nocheck"},
        )
        resp.raise_for_status()
        result = resp.json()

    results = result.get("results", [result])
    if results:
        att = results[0]
        return _text(f"Uploaded \"{att.get('title', path.name)}\" (id={att.get('id', '?')}) to page {page_id}.")
    return _text(f"Uploaded {path.name} to page {page_id}.")


@mcp.tool()
@_with_error_handling
async def confluence_set_restrictions(
    page_id: str,
    operation: str,
    users: list[str] = [],
    groups: list[str] = [],
) -> CallToolResult:
    """Set access restrictions on a Confluence page.

    Replaces existing restrictions for the given operation. To remove all
    restrictions, pass empty users and groups lists.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
        operation: The operation to restrict — "read" or "update".
        users: List of account IDs to grant access.
        groups: List of group names to grant access.
    """
    operation = operation.lower()
    if operation not in ("read", "update"):
        return _text(f"Invalid operation \"{operation}\". Use \"read\" or \"update\".")

    restrictions = []
    for user_id in users:
        restrictions.append({
            "type": "user",
            "accountId": user_id,
        })
    for group_name in groups:
        restrictions.append({
            "type": "group",
            "name": group_name,
        })

    async with _make_client(timeout=30.0) as client:
        page_id = await _resolve_page_id(client, page_id)

        payload = [{
            "operation": operation,
            "restrictions": {
                "user": [{"type": "known", "accountId": uid} for uid in users],
                "group": [{"type": "group", "name": gn} for gn in groups],
            },
        }]

        resp = await client.put(
            f"{BASE_URL}/rest/api/content/{page_id}/restriction",
            json=payload,
            auth=_auth(),
        )
        resp.raise_for_status()

    count = len(users) + len(groups)
    if count == 0:
        return _text(f"Cleared {operation} restrictions on page {page_id}.")
    return _text(f"Set {operation} restrictions: {len(users)} user(s), {len(groups)} group(s) on page {page_id}.")


@mcp.tool()
@_with_error_handling
async def confluence_watch_page(
    page_id: str,
    watch: bool = True,
) -> CallToolResult:
    """Watch or unwatch a Confluence page for the authenticated user.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
        watch: True to start watching, False to stop watching.
    """
    async with _make_client(timeout=30.0) as client:
        page_id = await _resolve_page_id(client, page_id)

        if watch:
            resp = await client.post(
                f"{BASE_URL}/rest/api/user/watch/content/{page_id}",
                auth=_auth(),
                headers={"X-Atlassian-Token": "nocheck", "Content-Type": "application/json"},
            )
        else:
            resp = await client.delete(
                f"{BASE_URL}/rest/api/user/watch/content/{page_id}",
                auth=_auth(),
            )

        resp.raise_for_status()

    action = "Watching" if watch else "Unwatched"
    return _text(f"{action} page {page_id}.")


@mcp.tool()
@_with_error_handling
async def confluence_revert_page(
    page_id: str,
    version_number: int,
    version_message: str = "",
) -> CallToolResult:
    """Revert a Confluence page to a previous version.

    Uses the v1 REST API restore operation to roll back a page.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
        version_number: The version number to revert to.
        version_message: Optional message describing the revert.
    """
    async with _make_client(timeout=30.0) as client:
        page_id = await _resolve_page_id(client, page_id)

        payload = {
            "operationKey": "restore",
            "params": {
                "versionNumber": version_number,
            },
        }
        if version_message:
            payload["params"]["message"] = version_message

        resp = await client.post(
            f"{BASE_URL}/rest/api/content/{page_id}/version",
            json=payload,
            auth=_auth(),
        )
        resp.raise_for_status()
        result = resp.json()

    return _text(f"Reverted to v{version_number}. Now at v{result['number']} — \"{result.get('message', '')}\".")


@mcp.tool()
@_with_error_handling
async def confluence_list_spaces(
    limit: int = 25,
    type: str = "",
    status: str = "current",
    cursor: str = "",
) -> CallToolResult:
    """List Confluence spaces.

    Args:
        limit: Maximum number of spaces to return (default 25, max 250).
        type: Filter by space type — "global" or "personal". Empty for all.
        status: Filter by status — "current" (default) or "archived".
        cursor: Pagination cursor from a previous result.
    """
    limit = min(limit, 250)
    params: dict = {"limit": limit, "status": status}
    if type:
        params["type"] = type
    if cursor:
        params["cursor"] = cursor

    async with _make_client(timeout=30.0) as client:
        resp = await client.get(
            f"{BASE_URL}/api/v2/spaces",
            params=params,
            auth=_auth(),
        )
        resp.raise_for_status()
        data = resp.json()

    spaces = data.get("results", [])
    if not spaces:
        return _text("No spaces found.")

    lines = [f"{len(spaces)} space(s):"]
    for s in spaces:
        sid = s.get("id", "?")
        name = s.get("name", "?")
        key = s.get("key", "?")
        stype = s.get("type", "?")
        lines.append(f"  [{sid}] \"{name}\" (key={key}, type={stype})")

    next_cursor = _extract_next_cursor(data)
    if next_cursor:
        lines.append(f"\nNext cursor: {next_cursor}")

    return _text("\n".join(lines))


@mcp.tool()
@_with_error_handling
async def confluence_archive_page(
    page_id: str,
    confirm: bool = False,
) -> CallToolResult:
    """Archive a Confluence page.

    DESTRUCTIVE: This removes the page from active view. By default this tool
    runs in preview mode — call with confirm=True to actually archive.

    You MUST show the preview to the user and get their explicit approval before
    calling again with confirm=True.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
        confirm: Must be True to actually archive. False (default) shows a preview.
    """
    async with _make_client(timeout=30.0) as client:
        page_id = await _resolve_page_id(client, page_id)
        data = await _get_page_raw(client, page_id)

        title = data["title"]
        version = data["version"]["number"]
        space_id = data.get("spaceId", "?")

        if not confirm:
            return _text(
                f"⚠ ARCHIVE PREVIEW — This will archive the following page:\n"
                f"  Page:    \"{title}\" (id={page_id})\n"
                f"  Space:   {space_id}\n"
                f"  Version: v{version}\n\n"
                f"The page will be removed from active view but can be restored.\n"
                f"To proceed, call again with confirm=True."
            )

        payload = {
            "id": page_id,
            "title": title,
            "status": "archived",
            "version": {"number": version + 1, "message": "Archived via MCP"},
            "body": {
                "representation": "atlas_doc_format",
                "value": data["body"]["atlas_doc_format"]["value"],
            },
        }
        resp = await client.put(
            f"{BASE_URL}/api/v2/pages/{page_id}",
            json=payload,
            auth=_auth(),
        )
        resp.raise_for_status()

    return _text(f"Archived \"{title}\" (id={page_id}).")


@mcp.tool()
@_with_error_handling
async def confluence_move_page(
    page_id: str,
    target_parent_id: str,
    confirm: bool = False,
) -> CallToolResult:
    """Move a Confluence page to a new parent.

    DESTRUCTIVE: This changes the page's location in the content tree. By default
    this tool runs in preview mode — call with confirm=True to actually move.

    You MUST show the preview to the user and get their explicit approval before
    calling again with confirm=True.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
        target_parent_id: The page ID of the new parent page.
        confirm: Must be True to actually move. False (default) shows a preview.
    """
    async with _make_client(timeout=30.0) as client:
        page_id = await _resolve_page_id(client, page_id)
        target_parent_id = await _resolve_page_id(client, target_parent_id)

        # Fetch both pages in parallel for context
        async def _fetch_source():
            return await _get_page_raw(client, page_id)

        async def _fetch_target():
            return await _get_page_raw(client, target_parent_id)

        source, target = await asyncio.gather(_fetch_source(), _fetch_target())

        src_title = source["title"]
        src_version = source["version"]["number"]
        src_space = source.get("spaceId", "?")
        tgt_title = target["title"]
        tgt_space = target.get("spaceId", "?")

        cross_space = src_space != tgt_space

        if not confirm:
            preview = (
                f"⚠ MOVE PREVIEW — This will move the following page:\n"
                f"  Page:        \"{src_title}\" (id={page_id})\n"
                f"  From space:  {src_space}\n"
                f"  To parent:   \"{tgt_title}\" (id={target_parent_id})\n"
                f"  In space:    {tgt_space}\n"
            )
            if cross_space:
                preview += f"\n  ⚠ This is a CROSS-SPACE move!\n"
            preview += f"\nTo proceed, call again with confirm=True."
            return _text(preview)

        result = await _push_page_update(
            client, page_id, src_title,
            _parse_adf(source), src_version,
            f"Moved under \"{tgt_title}\"",
        )

        # Update parent via v1 API (v2 PUT doesn't support parentId directly in all versions)
        move_payload = {
            "type": "page",
            "ancestors": [{"id": target_parent_id}],
        }
        resp = await client.put(
            f"{BASE_URL}/rest/api/content/{page_id}",
            json={
                "type": "page",
                "title": src_title,
                "version": {"number": result["version"]["number"] + 1},
                "ancestors": [{"id": target_parent_id}],
            },
            auth=_auth(),
        )
        resp.raise_for_status()

    msg = f"Moved \"{src_title}\" under \"{tgt_title}\" (id={target_parent_id})."
    if cross_space:
        msg += f" (cross-space: {src_space} → {tgt_space})"
    return _text(msg)


@mcp.tool()
@_with_error_handling
async def confluence_download_attachment(
    page_id: str,
    attachment_title: str,
    save_path: str,
) -> CallToolResult:
    """Download an attachment from a Confluence page to a local file.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
        attachment_title: The filename of the attachment to download (e.g. "report.pdf").
        save_path: Local file path to save the downloaded file.
    """
    async with _make_client(timeout=60.0) as client:
        page_id = await _resolve_page_id(client, page_id)

        # Find the attachment by title
        resp = await client.get(
            f"{BASE_URL}/api/v2/pages/{page_id}/attachments",
            params={"limit": 250},
            auth=_auth(),
        )
        resp.raise_for_status()
        attachments = resp.json().get("results", [])

        match = None
        for a in attachments:
            if a.get("title") == attachment_title:
                match = a
                break

        if not match:
            return _text(f"Attachment \"{attachment_title}\" not found on page {page_id}.")

        # Download via v1 download link
        download_url = f"{BASE_URL}/rest/api/content/{match['id']}/download"
        resp = await client.get(download_url, auth=_auth(), follow_redirects=True)
        resp.raise_for_status()

    dest = Path(save_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(resp.content)

    size_kb = len(resp.content) / 1024
    return _text(f"Downloaded \"{attachment_title}\" ({size_kb:.1f} KB) to {dest.resolve()}")


@mcp.tool()
@_with_error_handling
async def confluence_delete_attachment(
    page_id: str,
    attachment_title: str,
    confirm: bool = False,
) -> CallToolResult:
    """Delete an attachment from a Confluence page.

    DESTRUCTIVE: This permanently removes the attachment. By default this tool
    runs in preview mode — call with confirm=True to actually delete.

    You MUST show the preview to the user and get their explicit approval before
    calling again with confirm=True.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
        attachment_title: The filename of the attachment to delete.
        confirm: Must be True to actually delete. False (default) shows a preview.
    """
    async with _make_client(timeout=30.0) as client:
        page_id = await _resolve_page_id(client, page_id)

        resp = await client.get(
            f"{BASE_URL}/api/v2/pages/{page_id}/attachments",
            params={"limit": 250},
            auth=_auth(),
        )
        resp.raise_for_status()
        attachments = resp.json().get("results", [])

        match = None
        for a in attachments:
            if a.get("title") == attachment_title:
                match = a
                break

        if not match:
            return _text(f"Attachment \"{attachment_title}\" not found on page {page_id}.")

        att_id = match.get("id", "?")
        media_type = match.get("mediaType", "?")
        size = match.get("fileSize", 0)
        size_str = f"{size / 1024:.1f} KB" if size else "?"

        if not confirm:
            return _text(
                f"⚠ DELETE PREVIEW — This will permanently delete:\n"
                f"  File:  \"{attachment_title}\" (id={att_id})\n"
                f"  Type:  {media_type}\n"
                f"  Size:  {size_str}\n"
                f"  Page:  {page_id}\n\n"
                f"To proceed, call again with confirm=True."
            )

        resp = await client.delete(
            f"{BASE_URL}/rest/api/content/{att_id}",
            auth=_auth(),
        )
        resp.raise_for_status()

    return _text(f"Deleted attachment \"{attachment_title}\" (id={att_id}) from page {page_id}.")


@mcp.tool()
@_with_error_handling
async def confluence_list_inline_comments(
    page_id: str,
    limit: int = 25,
    cursor: str = "",
) -> CallToolResult:
    """List inline (annotation) comments on a Confluence page.

    These are comments anchored to specific text selections, as opposed to
    footer comments which appear at the bottom of the page.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
        limit: Maximum number of comments to return (default 25, max 100).
        cursor: Pagination cursor from a previous result.
    """
    limit = min(limit, 100)
    params: dict = {"limit": limit, "body-format": "atlas_doc_format"}
    if cursor:
        params["cursor"] = cursor

    async with _make_client(timeout=30.0) as client:
        page_id = await _resolve_page_id(client, page_id)
        resp = await client.get(
            f"{BASE_URL}/api/v2/pages/{page_id}/inline-comments",
            params=params,
            auth=_auth(),
        )
        resp.raise_for_status()
        data = resp.json()

    comments = data.get("results", [])
    if not comments:
        return _text("No inline comments on this page.")

    lines = [f"{len(comments)} inline comment(s):"]
    for c in comments:
        cid = c.get("id", "?")
        author = c.get("authorId", "?")
        created = c.get("createdAt", "?")
        props = c.get("properties", {}).get("inline-marker-ref", {})
        selection = ""
        if isinstance(props, dict):
            selection = props.get("value", "")
        body_adf = json.loads(
            c.get("body", {}).get("atlas_doc_format", {}).get("value", "{}")
        )
        text = _extract_text_from_adf(body_adf).strip()[:200]
        line = f"  [{cid}] by {author} at {created}: {text}"
        if selection:
            line += f" (on: \"{selection[:60]}\")"
        lines.append(line)

    next_cursor = _extract_next_cursor(data)
    if next_cursor:
        lines.append(f"\nNext cursor: {next_cursor}")

    return _text("\n".join(lines))


@mcp.tool()
@_with_error_handling
async def confluence_add_inline_comment(
    page_id: str,
    body: str,
    text_selection: str,
    match_index: int = 0,
) -> CallToolResult:
    """Add an inline (annotation) comment anchored to specific text on a page.

    The comment is attached to the first (or Nth) occurrence of text_selection
    found in the page body.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
        body: The comment text (plain text, converted to ADF paragraph).
        text_selection: The exact text on the page to attach the comment to.
        match_index: Which occurrence to annotate (0-based, default 0 = first match).
    """
    adf_body = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": body}],
            }
        ],
    }

    async with _make_client(timeout=30.0) as client:
        page_id = await _resolve_page_id(client, page_id)

        payload = {
            "pageId": page_id,
            "body": {
                "representation": "atlas_doc_format",
                "value": json.dumps(adf_body),
            },
            "inlineCommentProperties": {
                "textSelection": text_selection,
                "textSelectionMatchCount": match_index + 1,
                "textSelectionMatchIndex": match_index,
            },
        }

        resp = await client.post(
            f"{BASE_URL}/api/v2/inline-comments",
            json=payload,
            auth=_auth(),
        )
        resp.raise_for_status()
        result = resp.json()

    comment_id = result.get("id", "?")
    return _text(f"Added inline comment (id={comment_id}) on \"{text_selection[:60]}\" in page {page_id}.")


@mcp.tool()
@_with_error_handling
async def confluence_get_page_properties(
    page_id: str,
    limit: int = 25,
) -> CallToolResult:
    """Get content properties on a Confluence page.

    Content properties are key-value metadata pairs stored on pages.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
        limit: Maximum number of properties to return (default 25, max 100).
    """
    limit = min(limit, 100)
    async with _make_client(timeout=30.0) as client:
        page_id = await _resolve_page_id(client, page_id)
        resp = await client.get(
            f"{BASE_URL}/api/v2/pages/{page_id}/properties",
            params={"limit": limit},
            auth=_auth(),
        )
        resp.raise_for_status()
        data = resp.json()

    props = data.get("results", [])
    if not props:
        return _text("No properties on this page.")

    lines = [f"{len(props)} propert(ies):"]
    for p in props:
        key = p.get("key", "?")
        value = p.get("value", "")
        version = p.get("version", {}).get("number", "?")
        # Truncate long values
        val_str = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
        if len(val_str) > 120:
            val_str = val_str[:117] + "..."
        lines.append(f"  {key} = {val_str} (v{version})")

    return _text("\n".join(lines))


@mcp.tool()
@_with_error_handling
async def confluence_set_page_property(
    page_id: str,
    key: str,
    value: str,
) -> CallToolResult:
    """Set a content property on a Confluence page.

    Creates the property if it doesn't exist, or updates it if it does.
    The value is stored as a JSON string.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
        key: The property key (e.g. "status", "priority").
        value: The property value as a JSON string (e.g. '"done"', '{"score": 5}').
    """
    try:
        parsed_value = json.loads(value)
    except json.JSONDecodeError:
        # Treat as plain string
        parsed_value = value

    async with _make_client(timeout=30.0) as client:
        page_id = await _resolve_page_id(client, page_id)

        # Check if property already exists
        resp = await client.get(
            f"{BASE_URL}/api/v2/pages/{page_id}/properties",
            params={"limit": 100},
            auth=_auth(),
        )
        resp.raise_for_status()
        existing = resp.json().get("results", [])

        existing_prop = None
        for p in existing:
            if p.get("key") == key:
                existing_prop = p
                break

        if existing_prop:
            # Update existing
            prop_id = existing_prop["id"]
            current_version = existing_prop.get("version", {}).get("number", 1)
            payload = {
                "key": key,
                "value": parsed_value,
                "version": {"number": current_version + 1},
            }
            resp = await client.put(
                f"{BASE_URL}/api/v2/pages/{page_id}/properties/{prop_id}",
                json=payload,
                auth=_auth(),
            )
        else:
            # Create new
            payload = {
                "key": key,
                "value": parsed_value,
            }
            resp = await client.post(
                f"{BASE_URL}/api/v2/pages/{page_id}/properties",
                json=payload,
                auth=_auth(),
            )

        resp.raise_for_status()
        result = resp.json()

    action = "Updated" if existing_prop else "Created"
    ver = result.get("version", {}).get("number", "?")
    return _text(f"{action} property \"{key}\" on page {page_id} (v{ver}).")


@mcp.tool()
@_with_error_handling
async def confluence_copy_page(
    page_id: str,
    title: str = "",
    destination_parent_id: str = "",
    copy_labels: bool = True,
    copy_attachments: bool = True,
) -> CallToolResult:
    """Copy a Confluence page.

    Creates a duplicate of the page, optionally under a different parent.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
        title: Title for the copy. Defaults to "Copy of {original title}".
        destination_parent_id: Parent page ID for the copy. Empty = same parent.
        copy_labels: Whether to copy labels (default True).
        copy_attachments: Whether to copy attachments (default True).
    """
    async with _make_client(timeout=30.0) as client:
        page_id = await _resolve_page_id(client, page_id)
        data = await _get_page_raw(client, page_id)

        original_title = data["title"]
        copy_title = title or f"Copy of {original_title}"

        destination = {"type": "parent_page", "value": destination_parent_id} if destination_parent_id else None

        payload = {
            "copyAttachments": copy_attachments,
            "copyLabels": copy_labels,
            "copyPermissions": False,
            "destination": destination,
            "pageTitle": copy_title,
        }
        # Remove None destination
        if destination is None:
            del payload["destination"]

        resp = await client.post(
            f"{BASE_URL}/rest/api/content/{page_id}/copy",
            json=payload,
            auth=_auth(),
        )
        resp.raise_for_status()
        result = resp.json()

    new_id = result.get("id", "?")
    return _text(f"Copied \"{original_title}\" → \"{copy_title}\" (id={new_id}).")


@mcp.tool()
@_with_error_handling
async def confluence_get_user(
    account_id: str,
) -> CallToolResult:
    """Get user details by account ID.

    Resolves an account ID (as seen in version history, comments, etc.)
    to a display name, email, and profile info.

    Args:
        account_id: The Confluence/Atlassian account ID.
    """
    async with _make_client(timeout=30.0) as client:
        resp = await client.get(
            f"{BASE_URL}/rest/api/user",
            params={"accountId": account_id},
            auth=_auth(),
        )
        if resp.status_code == 404:
            return _text(f"User not found: {account_id}")
        resp.raise_for_status()
        user = resp.json()

    display_name = user.get("displayName", "?")
    account_type = user.get("accountType", "?")
    email = user.get("email", "")

    info = f"\"{display_name}\" (type={account_type}, id={account_id})"
    if email:
        info += f" — {email}"

    return _text(info)


@mcp.tool()
@_with_error_handling
async def confluence_list_cache() -> CallToolResult:
    """List all locally cached Confluence pages.

    Shows page IDs, titles, and when they were last cached.
    """
    if not CACHE_DIR.exists():
        return _text("Cache is empty.")
    files = sorted(CACHE_DIR.glob("*.json"))
    if not files:
        return _text("Cache is empty.")
    lines = [f"{len(files)} cached page(s):"]
    for f in files:
        data = json.loads(f.read_text())
        pid = data.get("id", f.stem)
        title = data.get("title", "?")
        mod_time = datetime.fromtimestamp(f.stat().st_mtime).isoformat()
        lines.append(f"  [{pid}] \"{title}\" (cached: {mod_time})")
    return _text("\n".join(lines))


@mcp.tool()
@_with_error_handling
async def confluence_clear_cache(page_id: str = "") -> CallToolResult:
    """Clear the local page cache.

    Removes cached page data. Pass a page_id to clear a specific page,
    or omit it to clear all cached pages.

    Args:
        page_id: Optional page ID to clear. Empty clears all cached pages.
    """
    if page_id:
        path = _cache_path(page_id)
        if path.exists():
            path.unlink()
            return _text(f"Cleared cache for page {page_id}.")
        return _text(f"No cache found for page {page_id}.")
    if CACHE_DIR.exists():
        count = 0
        for f in CACHE_DIR.glob("*.json"):
            f.unlink()
            count += 1
        return _text(f"Cleared {count} cached page(s).")
    return _text("Cache is already empty.")


if __name__ == "__main__":
    mcp.run()
