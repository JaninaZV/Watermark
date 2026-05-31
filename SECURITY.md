# Security Policy

## Reporting a vulnerability

If you discover a security issue, **please do not file a public GitHub issue**.

Email **janinavallon@gmail.com**
with:

- A description of the issue
- Steps to reproduce
- Impact assessment (what an attacker could do)
- Your suggested fix, if you have one

We will acknowledge receipt within 5 business days and work with you on a
coordinated disclosure timeline.

## What counts as a security issue

- Hardcoded credentials or API keys committed to the repository
- Remote code execution via meter input, dashboard templates, or portfolio HTML
- Code injection through unsanitized user input rendered in generated HTML
- Path traversal when reading run directories or writing output files
- Unsafe deserialization of untrusted JSON/CSV in a way that executes code

## What does not count as a security issue

- Debates about carbon/water methodology or regional constants
- Dashboard styling, layout, or UX preferences
- Accuracy of modeled energy when RAPL is unavailable on cloud VMs
- Missing features (per-process attribution, real-time grid API, hosted SaaS)

## Supported versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | Yes       |

## Secrets and API keys

Watermark reads optional API keys from environment variables only (e.g.
`ELECTRICITYMAPS_API_KEY`). Never commit `.env` files or paste keys into
issues or pull requests.
