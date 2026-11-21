# Changelog

All notable changes are recorded here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).
`infrastructure/scripts/release.py check` requires a section for the current version; the release workflow publishes that section as the GitHub Release notes.

## [Unreleased]

## [0.1.0] - 2026-11-21
### Added
- Phases 1–10: ingestion, hybrid retrieval, knowledge graph, multi-agent research workflow, chat/reports, memory, evaluation, observability, frontend.
- Phases 11–17: multimodal, enterprise connectors, autonomous agents, LLMOps (routing, budgets, prompt versions), AI security/governance, distributed operation, continuous learning.
- Phase 18: backup/restore drill, release tooling, CI/CD workflows, Kubernetes and AWS Terraform definitions, disaster-recovery runbooks.

### Notes
- First tagged release. Kubernetes manifests, Terraform and workflows were validated statically only; see `docs/releases.md` for what has and has not been exercised.
