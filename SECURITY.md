# Security Policy

This is a personal sample project. It is not an official Snowflake product.

## Reporting a vulnerability

Please report security issues privately through GitHub's "Report a vulnerability" button
(Security tab > Advisories) on this repository. Do not open a public issue for security problems.

Never commit credentials. The scripts read secrets from environment variables; copy
`.env.example` to `.env` (which is gitignored) and fill in your own values.
