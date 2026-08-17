# Minidisc Integration for Chronicle

## What Is Minidisc

[minidisc](https://github.com/mscheidegger/minidisc) is a zero-configuration service discovery library for Tailscale networks. It allows services on the same Tailnet to advertise themselves and be found without hardcoded IPs or DNS.

### How It Works

- **Peer-to-peer** — no central registry server
- **Leader/Delegate** pattern on each host — first process binds port 28004, others delegate
- **Full-scan discovery** — queries every online Tailnet peer at port 28004
- **Label-based matching** — services have name + key-value labels for filtering
- **Interoperable** — Go and Python implementations share the same HTTP/JSON protocol

### Data Model

```python
Service {
    name: str           # e.g., "chronicle-backend"
    labels: dict        # e.g., {"type": "api", "version": "1"}
    addrPort: str       # e.g., "100.64.1.5:8000"
}
```

### Python Usage

```python
import minidisc

# Server side — advertise
registry = minidisc.start_registry()
registry.advertise_service(port=8000, name="chronicle-backend", labels={"type": "api"})

# Client side — discover
endpoint = minidisc.find_service("chronicle-backend")
# Returns "100.64.1.5:8000"
```

### Requirements

- Tailscale running on the machine
- Access to `/var/run/tailscale/tailscaled.sock`
- Python: `pip install minidisc-python` (pydantic 2.x dependency — already used by Chronicle)

## Integration Points

### 1. Backend Startup — Advertise Services

In `app_factory.py` or `main.py`, the backend advertises itself when Tailscale is detected:

```python
import minidisc

def advertise_chronicle_services():
    """Advertise Chronicle services on the Tailnet."""
    try:
        registry = minidisc.start_registry()
        registry.advertise_service(
            port=8000,
            name="chronicle-backend",
            labels={"type": "api"}
        )
        registry.advertise_service(
            port=8000,
            name="chronicle-backend",
            labels={"type": "ws"}
        )
        logger.info("Advertised Chronicle backend on Tailnet via minidisc")
        return registry
    except Exception as e:
        logger.info(f"minidisc not available (not on Tailnet?): {e}")
        return None
```

### 2. Service URL Resolution — Discover Optional Services

A helper that checks `.env` first, falls back to minidisc discovery:

```python
def resolve_service_url(env_var: str, service_name: str, default: str | None = None) -> str | None:
    """Resolve a service URL from env, falling back to minidisc discovery."""
    # Env var takes precedence (for Docker/local setups)
    url = os.getenv(env_var)
    if url:
        return url

    # Try minidisc discovery (for distributed Tailscale setups)
    try:
        import minidisc
        endpoint = minidisc.find_service(service_name)
        if endpoint:
            return f"http://{endpoint}"
    except Exception:
        pass

    return default


# Usage in config resolution:
SPEAKER_SERVICE_URL = resolve_service_url("SPEAKER_SERVICE_URL", "speaker-service")
PARAKEET_ASR_URL = resolve_service_url("PARAKEET_ASR_URL", "parakeet-asr")
OPENMEMORY_MCP_URL = resolve_service_url("OPENMEMORY_MCP_URL", "openmemory-mcp")
```

### 3. Optional Services — Advertise Themselves

Each optional service advertises when started:

**Speaker Recognition** (`extras/speaker-recognition/`):
```python
registry.advertise_service(port=8085, name="speaker-service")
```

**Parakeet ASR** (`extras/asr-services/`):
```python
registry.advertise_service(port=8767, name="parakeet-asr")
```

**OpenMemory MCP** (`extras/openmemory-mcp/`):
```python
registry.advertise_service(port=8765, name="openmemory-mcp")
```

### 4. HAVPE Relay / Chronicle Edge — Discover Backend

The relay discovers the backend instead of requiring `--backend-url`:

```python
import minidisc

def find_chronicle_backend() -> tuple[str, str]:
    """Find Chronicle backend on the Tailnet."""
    endpoint = minidisc.find_service("chronicle-backend", {"type": "api"})
    if not endpoint:
        raise RuntimeError("Chronicle backend not found on Tailnet")

    http_url = f"http://{endpoint}"
    ws_url = f"ws://{endpoint}"
    return http_url, ws_url
```

## Service Registry

All Chronicle services that should be discoverable:

| Service | minidisc name | Labels | Port |
|---|---|---|---|
| Backend API | `chronicle-backend` | `{"type": "api"}` | 8000 |
| Backend WebSocket | `chronicle-backend` | `{"type": "ws"}` | 8000 |
| Speaker Recognition | `speaker-service` | `{}` | 8085 |
| Parakeet ASR | `parakeet-asr` | `{}` | 8767 |
| OpenMemory MCP | `openmemory-mcp` | `{}` | 8765 |

## Docker Considerations

For services running in Docker containers, minidisc needs the Tailscale socket mounted:

```yaml
# docker-compose.yml
services:
  chronicle-backend:
    volumes:
      - /var/run/tailscale/tailscaled.sock:/var/run/tailscale/tailscaled.sock:ro
```

Alternatively, the minidisc registry can run on the host (outside Docker) and advertise the containerized services' host-mapped ports.

## Fallback Behavior

The integration is designed to be **additive, not required**:

1. If Tailscale is not installed → minidisc silently disabled, `.env` values used
2. If minidisc can't find a service → falls back to `.env` or default
3. Single-machine Docker setups → no Tailscale needed, Docker networking handles it
4. Distributed setups → Tailscale + minidisc handles everything automatically

This means the existing setup flow continues to work unchanged for users who don't use Tailscale.

## Wizard Simplification

When Tailscale is detected, the wizard can skip network configuration entirely:

```
Current wizard flow:
  1. Auth credentials
  2. Transcription provider + API key
  3. LLM provider + API key
  4. Memory provider
  5. HOST_IP configuration          <-- eliminated
  6. Service URLs (speaker, ASR)    <-- eliminated
  7. CORS origins                   <-- auto-derived
  8. HTTPS/SSL setup               <-- Tailscale HTTPS certs

Simplified wizard flow (with Tailscale):
  1. Auth credentials
  2. Transcription provider + API key
  3. LLM provider + API key
  4. Memory provider
  Done.
```

## Limitations

- **Discovery latency**: Full Tailnet scan on each `find_service()` call. Fine for startup, not for hot-path requests. Cache results.
- **No health checking**: minidisc only checks port 28004, not the actual service. Chronicle's `/health` endpoints still needed.
- **Library maturity**: minidisc describes itself as having "only little mileage." Monitor for issues.
- **Phone app**: React Native can't use minidisc (Python/Go only). QR code pairing covers this.
- **Large Tailnets**: Full scan is O(N) peers. For small home networks (2-10 devices), this is fine.
