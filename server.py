import asyncio
import json
import os
import re
import time
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


@mcp.tool()
async def confluence_get_page(page_id: str) -> CallToolResult:
    """Fetch a Confluence page and cache it locally for editing.

    Returns the page metadata and the local cache file path. Edit the cached file
    directly, then call confluence_push_page to publish your changes.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
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

    async with httpx.AsyncClient(timeout=30.0) as client:
        result = await _push_page_update(
            client, page_id, cached["title"], cached["adf"],
            cached["version"], version_message or "Updated via MCP",
        )

    _cache_after_push(result, cached["adf"], cached.get("spaceId"))

    return _text(f"Pushed \"{result['title']}\" to v{result['version']['number']}.")


@mcp.tool()
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

    async with httpx.AsyncClient(timeout=30.0) as client:
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

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{BASE_URL}/api/v2/pages",
            json=payload,
            auth=_auth(),
        )
        resp.raise_for_status()
        result = resp.json()

    return _text(f"Created \"{result['title']}\" (v{result['version']['number']}, id={result['id']}).")


@mcp.tool()
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

    async with httpx.AsyncClient(timeout=30.0) as client:
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
async def confluence_search_pages(
    query: str,
    limit: int = 10,
) -> CallToolResult:
    """Search Confluence pages using CQL (Confluence Query Language).

    Returns page titles, IDs, and spaces matching the query. Supports CQL operators
    like AND, OR, ~, =, etc. Simple text is treated as a title/content search.

    Args:
        query: CQL query string, e.g. 'type=page AND title~"meeting notes"' or just "meeting notes".
        limit: Maximum number of results to return (default 10, max 50).
    """
    limit = min(limit, 50)
    # If the query doesn't contain CQL operators, wrap it as a text search
    cql = query if any(op in query for op in ("=", "~", " AND ", " OR ", " IN ")) else f'type=page AND (title~"{query}" OR text~"{query}")'

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{BASE_URL}/rest/api/search",
            params={"cql": cql, "limit": limit},
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

    return _text("\n".join(lines))


@mcp.tool()
async def confluence_list_pages(
    space_id: str,
    limit: int = 25,
    sort: str = "title",
) -> CallToolResult:
    """List pages in a Confluence space.

    Args:
        space_id: The numeric space ID.
        limit: Maximum number of pages to return (default 25, max 250).
        sort: Sort order — "title", "-title", "created-date", "-modified-date", etc.
    """
    limit = min(limit, 250)
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{BASE_URL}/api/v2/spaces/{space_id}/pages",
            params={"limit": limit, "sort": sort},
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

    return _text("\n".join(lines))


@mcp.tool()
async def confluence_get_child_pages(
    page_id: str,
    limit: int = 25,
) -> CallToolResult:
    """Get child pages of a Confluence page.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
        limit: Maximum number of children to return (default 25, max 250).
    """
    limit = min(limit, 250)
    async with httpx.AsyncClient(timeout=30.0) as client:
        page_id = await _resolve_page_id(client, page_id)
        resp = await client.get(
            f"{BASE_URL}/api/v2/pages/{page_id}/children",
            params={"limit": limit},
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

    return _text("\n".join(lines))


@mcp.tool()
async def confluence_get_ancestors(
    page_id: str,
) -> CallToolResult:
    """Get the ancestor (parent) chain of a Confluence page.

    Returns the page hierarchy from the space root down to the immediate parent.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
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
async def confluence_get_labels(
    page_id: str,
) -> CallToolResult:
    """Get all labels on a Confluence page.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
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
async def confluence_add_labels(
    page_id: str,
    labels: list[str],
) -> CallToolResult:
    """Add labels to a Confluence page.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
        labels: List of label names to add, e.g. ["important", "reviewed"].
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
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
async def confluence_remove_label(
    page_id: str,
    label: str,
) -> CallToolResult:
    """Remove a label from a Confluence page.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
        label: The label name to remove.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
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
async def confluence_list_versions(
    page_id: str,
    limit: int = 10,
) -> CallToolResult:
    """List version history of a Confluence page.

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
        limit: Maximum number of versions to return (default 10, max 50).
    """
    limit = min(limit, 50)
    async with httpx.AsyncClient(timeout=30.0) as client:
        page_id = await _resolve_page_id(client, page_id)
        resp = await client.get(
            f"{BASE_URL}/api/v2/pages/{page_id}/versions",
            params={"limit": limit},
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

    return _text("\n".join(lines))


@mcp.tool()
async def confluence_extract_text(
    page_id: str,
) -> CallToolResult:
    """Extract plain text content from a Confluence page.

    Fetches the page ADF and converts it to readable plaintext with basic
    formatting (paragraphs, bullet lists, tables, code blocks, etc.).

    Args:
        page_id: A numeric page ID or a Confluence URL (including short /wiki/x/ links).
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        page_id = await _resolve_page_id(client, page_id)
        data = await _get_page_raw(client, page_id)

    adf = _parse_adf(data)
    text = _extract_text_from_adf(adf)

    title = data.get("title", "?")
    return _text(f"# {title}\n\n{text.strip()}")


@mcp.tool()
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
    async with httpx.AsyncClient(timeout=30.0) as client:
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


if __name__ == "__main__":
    mcp.run()
