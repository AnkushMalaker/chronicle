# Test Environment Configuration
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Load environment files with correct precedence:
# 1. Environment variables (highest priority - from shell, CI, etc.)
# 2. .env.test (test-specific configuration)
# 3. .env (default configuration)

# Find repository root (tests/setup/test_env.py -> go up 2 levels)
REPO_ROOT = Path(__file__).parent.parent.parent
backend_dir = REPO_ROOT / "backends" / "advanced"
tests_dir = REPO_ROOT / "tests"

# Export absolute paths for Robot Framework keywords
BACKEND_DIR = str(backend_dir.absolute())
REPO_ROOT_DIR = str(REPO_ROOT.absolute())
SPEAKER_RECOGNITION_DIR = str((REPO_ROOT / "extras" / "speaker-recognition").absolute())

# Load in reverse order of precedence (since override=False won't overwrite existing vars)
# Load .env.test first (will set test-specific values)
# Try tests/setup/.env.test first, then fall back to tests/.env.test
load_dotenv(Path(__file__).parent / ".env.test", override=False)
load_dotenv(tests_dir / ".env.test", override=False)

# Load .env second (will only fill in missing values, won't override .env.test or existing env vars)
load_dotenv(backend_dir / ".env", override=False)

# Final precedence: environment variables > .env.test > .env

# API Configuration
API_URL = "http://localhost:8001"  # Use BACKEND_URL from test.env
API_BASE = "http://localhost:8001/api"
SPEAKER_RECOGNITION_URL = "http://localhost:8085"  # Speaker recognition service

WEB_URL = os.getenv(
    "FRONTEND_URL", "http://localhost:3001"
)  # Use FRONTEND_URL from test.env

# Test-specific credentials (override any values from .env)
# These are the credentials used in docker-compose-test.yml
ADMIN_EMAIL = "test-admin@example.com"
ADMIN_PASSWORD = "test-admin-password-123"

# Admin user credentials (Robot Framework format)
ADMIN_USER = {"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}

TEST_USER = {"email": "test@example.com", "password": "test-password"}

# Individual variables for Robot Framework
TEST_USER_EMAIL = "test@example.com"
TEST_USER_PASSWORD = "test-password"


# API Endpoints
ENDPOINTS = {
    "health": "/health",
    "readiness": "/readiness",
    "auth": "/auth/jwt/login",
    "conversations": "/api/conversations",
    "memories": "/api/memories",
    "memory_search": "/api/memories/search",
    "users": "/api/users",
}

# API Keys (loaded from test.env)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
HF_TOKEN = os.getenv("HF_TOKEN")

# Test Configuration
TEST_CONFIG = {"retry_count": 3, "retry_delay": 1, "default_timeout": 30}


# Container engine. Same precedence as tests/bin/_engine.sh and services.py:
# CONTAINER_ENGINE env -> config/config.yml container_engine -> docker default.
# Reading config.yml matters because a podman host selects the engine there, not
# in the environment; without it the keywords shell out to a `docker` binary that
# does not exist and every container-touching test fails for a reason that has
# nothing to do with what it is testing.
def _detect_container_engine() -> str:
    from_env = os.getenv("CONTAINER_ENGINE")
    if from_env:
        return from_env

    config_path = REPO_ROOT / "config" / "config.yml"
    if config_path.exists():
        for line in config_path.read_text().splitlines():
            if line.startswith("container_engine:"):
                return line.split(":", 1)[1].strip().strip("\"'")

    return "docker"


CONTAINER_ENGINE = _detect_container_engine()


# The LLM the ACTIVE PROFILE configures -- never a hardcoded vendor endpoint.
#
# Some verification keywords ask an LLM to judge output quality. Pointing those
# at api.openai.com directly means the stub profile still makes real, billable
# calls (and fails with 429 when rate-limited), which defeats the point of having
# profiles at all. Resolve the endpoint from the same config the backend is
# running with instead.
def _active_llm() -> dict:
    config_name = os.path.basename(os.getenv("TEST_CONFIG_FILE", "")) or None
    if not config_name:
        profile = os.getenv("PROFILE") or "mock"
        manifest = REPO_ROOT / "tests" / "profiles.yml"
        try:
            profiles = yaml.safe_load(manifest.read_text())["profiles"]
            config_name = os.path.basename(profiles[profile]["config"])
        except (OSError, KeyError, TypeError):
            config_name = "mock-services.yml"

    config_path = REPO_ROOT / "tests" / "configs" / config_name
    try:
        config = yaml.safe_load(config_path.read_text())
        wanted = config["defaults"]["llm"]
        model = next(m for m in config["models"] if m.get("name") == wanted)
    except (OSError, KeyError, StopIteration, TypeError):
        return {
            "base": "http://localhost:11435/v1",
            "key": "not-used",
            "model": "gpt-4o-mini",
        }

    # Configs address services as the BACKEND sees them; Robot runs on the host,
    # where those same services are published on localhost.
    base = str(model.get("model_url", "")).replace("host.docker.internal", "localhost")

    key = str(model.get("api_key", ""))
    if key.startswith("${oc.env:"):
        var, _, default = key[len("${oc.env:") : -1].partition(",")
        key = os.getenv(var.strip(), default.strip())

    return {
        "base": base,
        "key": key or "not-used",
        "model": model.get("model_name", ""),
    }


_llm = _active_llm()
LLM_API_BASE = _llm["base"]
LLM_API_KEY = _llm["key"]
LLM_MODEL = _llm["model"]

# Container names (docker-compose-test.yml project name: backend-test).
# docker compose joins with "-", podman-compose with "_".
_SEP = "_" if CONTAINER_ENGINE == "podman" else "-"


def _container_name(service: str) -> str:
    return f"backend-test{_SEP}{service}{_SEP}1"


BACKEND_CONTAINER = _container_name("chronicle-backend-test")
WORKERS_CONTAINER = _container_name("workers-test")
MONGO_CONTAINER = _container_name("mongo-test")
REDIS_CONTAINER = _container_name("redis-test")
WEBUI_CONTAINER = _container_name("webui-test")
