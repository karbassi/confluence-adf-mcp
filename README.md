# confluence-adf-mcp

MCP server for reading and writing Confluence pages in native ADF (Atlassian Document Format).

## Setup

### Requirements

- Python 3.12+
- A Confluence Cloud instance with API access

### Environment variables

```bash
export CONFLUENCE_URL="https://your-domain.atlassian.net/wiki"
export CONFLUENCE_USERNAME="you@example.com"
export CONFLUENCE_API_TOKEN="your-api-token"
```

Generate an API token at https://id.atlassian.com/manage-profile/security/api-tokens.

### Install and run

```bash
uv sync
uv run server.py
```

### Claude Code configuration

Add to `.claude/settings.json`:

```json
{
  "mcpServers": {
    "confluence-adf": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/confluence-adf-mcp", "server.py"],
      "env": {
        "CONFLUENCE_URL": "https://your-domain.atlassian.net/wiki",
        "CONFLUENCE_USERNAME": "you@example.com",
        "CONFLUENCE_API_TOKEN": "your-api-token"
      }
    }
  }
}
```

## Tools

### Pages

| Tool | Description |
|------|-------------|
| `confluence_get_page` | Fetch a page and cache it locally |
| `confluence_create_page` | Create a new page with ADF content |
| `confluence_push_page` | Push cached page edits to Confluence |
| `confluence_extract_text` | Extract plain text from a page |
| `confluence_copy_page` | Duplicate a page |
| `confluence_archive_page` | Archive a page (with confirmation) |
| `confluence_move_page` | Move a page to a new parent (with confirmation) |
| `confluence_revert_page` | Revert a page to a previous version |

### Editing

| Tool | Description |
|------|-------------|
| `confluence_edit_page` | Find/replace text in cached page |
| `confluence_find_replace` | Fetch, find/replace, and push in one step |
| `confluence_regex_replace` | Regex find/replace on a page |
| `confluence_replace_mention` | Swap @mentions between users |
| `confluence_add_link` | Add a hyperlink to a page |

### Tables

| Tool | Description |
|------|-------------|
| `confluence_update_table_cell` | Update a single table cell |
| `confluence_insert_table_row` | Insert a row into a table |
| `confluence_delete_table_row` | Delete a row from a table |

### Tasks

| Tool | Description |
|------|-------------|
| `confluence_update_task` | Toggle task checkbox state (DONE/TODO) |

### Discovery

| Tool | Description |
|------|-------------|
| `confluence_search_pages` | Search pages with CQL |
| `confluence_list_pages` | List pages in a space |
| `confluence_get_child_pages` | Get child pages |
| `confluence_get_ancestors` | Get parent chain |
| `confluence_list_spaces` | List spaces |
| `confluence_get_contributors` | Get unique page contributors |
| `confluence_get_user` | Resolve account ID to display name |

### Labels

| Tool | Description |
|------|-------------|
| `confluence_get_labels` | Get labels on a page |
| `confluence_add_labels` | Add labels to a page |
| `confluence_remove_label` | Remove a label from a page |

### Versions

| Tool | Description |
|------|-------------|
| `confluence_list_versions` | List version history |
| `confluence_compare_versions` | Diff two versions as text |

### Comments

| Tool | Description |
|------|-------------|
| `confluence_add_comment` | Add a footer comment |
| `confluence_list_comments` | List footer comments |
| `confluence_add_inline_comment` | Add an inline annotation comment |
| `confluence_list_inline_comments` | List inline comments |

### Attachments

| Tool | Description |
|------|-------------|
| `confluence_list_attachments` | List attachments on a page |
| `confluence_upload_attachment` | Upload a file as an attachment |
| `confluence_download_attachment` | Download an attachment to a local file |
| `confluence_delete_attachment` | Delete an attachment (with confirmation) |

### Properties

| Tool | Description |
|------|-------------|
| `confluence_get_page_properties` | Get content properties |
| `confluence_set_page_property` | Set a content property |

### Access Control

| Tool | Description |
|------|-------------|
| `confluence_set_restrictions` | Set read/update restrictions |
| `confluence_watch_page` | Watch or unwatch a page |

### Cache

| Tool | Description |
|------|-------------|
| `confluence_list_cache` | List locally cached pages |
| `confluence_clear_cache` | Clear page cache |

## Development

```bash
# Install with test dependencies
uv sync --extra test

# Run tests
uv run pytest tests/ -v
```
