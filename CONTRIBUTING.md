# Contributing

## Setup

```bash
git clone https://github.com/karbassi/confluence-adf-mcp.git
cd confluence-adf-mcp
cp .env.example .env   # fill in your credentials
uv sync --extra test
```

## Running tests

```bash
uv run pytest tests/ -v
```

All tests must pass before submitting a PR.

## Local MCP config

Point Claude Code at your local checkout instead of the remote repo:

```json
{
  "mcpServers": {
    "confluence-adf": {
      "command": "uvx",
      "args": ["--from", "/path/to/confluence-adf-mcp", "confluence-adf-mcp"]
    }
  }
}
```

## Commits

- Follow [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`)
- Keep commits small and focused — one logical change per commit
- Version follows [Semantic Versioning](https://semver.org/)

## Pull requests

1. Create a branch from `main`
2. Make your changes
3. Run tests
4. Open a PR with a clear description of what and why

## Project structure

| Path | Description |
|------|-------------|
| `server.py` | MCP server — all tools and helpers |
| `tests/` | Test suite (pytest + respx) |
| `.env.example` | Template for credentials |
| `CHANGELOG.md` | Release notes ([Keep a Changelog](https://keepachangelog.com/en/1.0.0/)) |
