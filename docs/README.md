# Chronicle Documentation

This directory is the canonical home for Chronicle's technical documentation.
Start with the repository [Quick Start Guide](../quickstart.md) for installation and
day-to-day operation, and use [AGENTS.md](../AGENTS.md) for development conventions.

## System

- [Project overview](overview.md): components, deployment topology, and repository layout
- [Testing and coverage](testing.md): fast Python lanes, coverage reports, and integration tests
- [Release process](releasing.md): candidate validation, TestFlight, publication, and verification
- [Audio pipeline](audio-pipeline-architecture.md): session, transcription, and memory flow
- [Multimodal memory](multimodal-memory.md): observations, event discovery, evidence retrieval, and durable memory
- [Initialization system](init-system.md): setup wizard and service orchestration
- [Fleet updates](fleet-updates.md): how nodes learn about and apply code updates
- [ScreenPipe capture nodes](screenpipe.md): local capture, Chronicle ingestion, desktop controls, and logs
- [Podman](podman.md): rootless containers, GPU access, and engine migration
- [SSL certificates](ssl-certificates.md): HTTPS setup and certificate trust

## Backend

- [Compose stack](backend/compose-stack.md): the backend's containers, shared mounts, and profiles
- [Authentication](backend/auth.md): user identity, JWTs, and protected endpoints
- [Memory system](backend/memories.md): agentic Markdown vault and retrieval
- [Semantic timeline episodes](backend/timeline-episodes.md): evidence compaction, agent analysis, and revisioned day views
- [Obsidian companion](obsidian-companion.md): deterministic vault maintenance from Obsidian and agent skills
- [Audio durability](backend/audio-durability.md): raw-audio write path and its state machine
- [Data archive and memory rebuild](backend/data-archive.md): full export/import and clean vault reconstruction
- [Plugin configuration](backend/plugin-configuration.md): configuration and secret boundaries
- [Plugin development](backend/plugin-development-guide.md): plugin lifecycle and APIs

## Contributing

- [Frontend UX review notes](agents/frontend-ux-review.md): Chronicle-specific UI conventions

## Research

Point-in-time investigations. Each states its own date and scope; they are kept for
their evidence, not as descriptions of the current system.

- [Codex vault evaluation](codex-vault-evaluation-2026-07-17.md): Codex CLI memory
  executor compared against the direct tool-calling vault agent
- [Memory rebuild audit](memory-rebuild-audit-2026-07-16.md): read-only audit of a
  live database and its generated vault
- [Vault iteration plan](vault-iteration-plan-2026-07-16.md): follow-up to the
  rebuild audit

Design plans (`docs/plans/`) and the screen-memory research reports
(`docs/research/`) are kept locally and are not part of the repository.

Service-specific instructions remain beside their implementations, such as
[`extras/speaker-recognition/`](../extras/speaker-recognition/README.md) and
[`extras/tts/`](../extras/tts/README.md).
