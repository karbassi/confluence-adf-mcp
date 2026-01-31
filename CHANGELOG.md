# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `confluence_list_spaces` tool for space discovery
- `confluence_archive_page` tool with two-step confirmation guard
- `confluence_move_page` tool with two-step confirmation and cross-space warning
- Comprehensive test suite (164 tests) covering all functions and MCP tools
- `CLAUDE.md` with SDLC conventions

### Changed

- Destructive tools (archive, move) require `confirm=True` to execute

## [0.1.0] - 2025-01-01

### Added

- Core page operations: `get_page`, `edit_page`, `push_page`, `find_replace`, `create_page`
- User mention replacement: `replace_mention`
- Page discovery: `search_pages`, `list_pages`, `get_child_pages`, `get_ancestors`
- Labels: `get_labels`, `add_labels`, `remove_label`
- Versions: `list_versions`, `revert_page`, `compare_versions`
- Text extraction: `extract_text`
- Task management: `update_task`
- Advanced text: `regex_replace`
- Table operations: `update_table_cell`, `insert_table_row`, `delete_table_row`
- Comments: `add_comment`, `list_comments`
- Attachments: `list_attachments`, `upload_attachment`
- Links: `add_link`
- Access control: `set_restrictions`, `watch_page`
- Contributors: `get_contributors`
- Local ADF caching with `.cache/confluence/` directory
- 409 conflict auto-retry on page updates
