# Security Policy

NEXUS AI is a research/portfolio project under active development and has **not** been independently
audited. Do not expose it to the internet with real data without your own review.

Report vulnerabilities privately via GitHub Security Advisories on this repository rather than public issues.

Implemented controls, tests and residual risks are listed in [`docs/security/threat-model.md`](docs/security/threat-model.md) (design-level review by the authors, not an audit).
Secrets are read only from `NEXUS_*` environment variables; `.env` is git-ignored.
