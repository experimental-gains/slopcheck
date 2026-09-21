# Security Policy

## Supported versions

Only the latest release on PyPI is supported. Upgrade before reporting
an issue that a newer version might already fix:

```
pip install --upgrade slopcheck
```

## Reporting a vulnerability

Email **ops@maelstrom.build** with what you found and, if possible, a
way to reproduce it. There's no bug bounty — this is a small
single-maintainer tool — but reports are read and fixed.

## Dependency vulnerability status

`slopcheck`'s only runtime dependency is `tomli` (only installed on
Python < 3.11, where the standard library has no built-in TOML
parser). Checked against `api.osv.dev` on 2026-09-21: zero known
vulnerabilities in the currently pinned range.
