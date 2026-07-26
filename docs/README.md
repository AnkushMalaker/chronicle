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
- [ScreenPipe capture nodes](screenpipe.md): local capture, Chronicle ingestion, desktop controls, and logs
- [Podman](podman.md): rootless containers, GPU access, and engine migration
- [SSL certificates](ssl-certificates.md): HTTPS setup and certificate trust

## Backend

- [Authentication](backend/auth.md): user identity, JWTs, and protected endpoints
- [Memory system](backend/memories.md): agentic Markdown vault and retrieval
- [Data archive and memory rebuild](backend/data-archive.md): full export/import and clean vault reconstruction
- [Plugin configuration](backend/plugin-configuration.md): configuration and secret boundaries
- [Plugin development](backend/plugin-development-guide.md): plugin lifecycle and APIs

## Research and plans

- [Screen event extraction plan](plans/screen-event-extraction.md): how screen
  capture becomes events and memories, and which of the design doc's assumptions
  the measurements changed
- [Screen memory research](research/screen-memory/): what ScreenPipe's own memory
  does, the published techniques for finding what matters in a capture stream, a
  VideoDB evaluation, and measured results from six extraction prototypes

Service-specific instructions remain beside their implementations, such as
[`extras/speaker-recognition/`](../extras/speaker-recognition/README.md) and
[`extras/tts/`](../extras/tts/README.md).
