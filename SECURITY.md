# Security Policy

## Reporting a vulnerability

If you discover a security vulnerability, please [open a private security advisory](https://github.com/karbassi/confluence-adf-mcp/security/advisories/new) rather than a public issue.

## Credentials

- Never commit `.env` files or API tokens
- The `.env` file is gitignored by default
- OAuth token files (`.cache/confluence/.oauth_tokens.json`) are written with `0600` permissions
