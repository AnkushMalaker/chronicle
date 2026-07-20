# Chronicle Documentation

This directory is the canonical home for Chronicle's technical documentation.
Start with the repository [Quick Start Guide](../quickstart.md) for installation and
day-to-day operation, and use [AGENTS.md](../AGENTS.md) for development conventions.

## System

- [Project overview](overview.md): components, deployment topology, and repository layout
- [Testing and coverage](testing.md): fast Python lanes, coverage reports, and integration tests
- [Audio pipeline](audio-pipeline-architecture.md): session, transcription, and memory flow
- [Initialization system](init-system.md): setup wizard and service orchestration
- [Podman](podman.md): rootless containers, GPU access, and engine migration
- [SSL certificates](ssl-certificates.md): HTTPS setup and certificate trust

## Backend

- [Authentication](backend/auth.md): user identity, JWTs, and protected endpoints
- [Memory system](backend/memories.md): agentic Markdown vault and retrieval
- [Data archive and memory rebuild](backend/data-archive.md): full export/import and clean vault reconstruction
- [Plugin configuration](backend/plugin-configuration.md): configuration and secret boundaries
- [Plugin development](backend/plugin-development-guide.md): plugin lifecycle and APIs

Service-specific instructions remain beside their implementations, such as
[`extras/speaker-recognition/`](../extras/speaker-recognition/README.md) and
[`extras/tts/`](../extras/tts/README.md).
