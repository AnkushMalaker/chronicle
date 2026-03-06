"""Tests for the Docker image versioning & GHCR deployment feature.

Covers three areas without requiring Docker or network access:
  1. services.py  --use-prebuilt flag (argument parsing + env-var injection)
  2. docker-compose.yml files contain the expected image: fields
  3. push-images.sh / pull-images.sh reject missing inputs
"""

import importlib
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"

# ---------------------------------------------------------------------------
# Helper: import services.py from the repo root (it lives there, not in a pkg).
# services.py depends on python-dotenv and rich which may not be installed in
# the lightweight test environment, so we stub them at sys.modules level.
# ---------------------------------------------------------------------------


def _stub_missing(name: str, attrs: dict):
    """Insert a minimal fake module under *name* if it isn't already importable."""
    if name in sys.modules:
        return
    fake = MagicMock()
    for k, v in attrs.items():
        setattr(fake, k, v)
    sys.modules[name] = fake


def _import_services():
    # Stub third-party deps that aren't installed in the bare test runner
    _stub_missing("dotenv", {"dotenv_values": lambda path: {}})
    _stub_missing("rich", {})
    _stub_missing("rich.console", {"Console": MagicMock})
    _stub_missing("rich.table", {"Table": MagicMock})
    _stub_missing("setup_utils", {"read_env_value": lambda *a, **kw: None})

    spec = importlib.util.spec_from_file_location("services", REPO_ROOT / "services.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ===========================================================================
# 1. services.py — --use-prebuilt flag
# ===========================================================================


class TestUsePrebuiltFlag:
    """Test the --use-prebuilt TAG argument added to services.py start command."""

    def _run_start(self, argv, env_override=None):
        """Import and run services.main() with given argv + env, mocking side effects."""
        services_mod = _import_services()

        override = env_override or {}
        env = {**os.environ, **override}
        # Remove CHRONICLE_* vars so we start clean, unless explicitly provided
        if "CHRONICLE_REGISTRY" not in override:
            env.pop("CHRONICLE_REGISTRY", None)
        if "CHRONICLE_TAG" not in override:
            env.pop("CHRONICLE_TAG", None)

        captured_calls = {}

        def fake_start_services(service_list, build, force_recreate):
            captured_calls["service_list"] = service_list
            captured_calls["build"] = build
            captured_calls["force_recreate"] = force_recreate
            # Capture env vars here while patch.dict is still active
            captured_calls["CHRONICLE_REGISTRY"] = os.environ.get("CHRONICLE_REGISTRY")
            captured_calls["CHRONICLE_TAG"] = os.environ.get("CHRONICLE_TAG")

        with (
            patch.object(
                services_mod, "start_services", side_effect=fake_start_services
            ),
            patch.object(services_mod, "check_service_configured", return_value=True),
            patch.object(services_mod, "ensure_docker_network", return_value=True),
            patch.object(
                services_mod, "_langfuse_enabled_in_backend", return_value=False
            ),
            patch.dict(os.environ, env, clear=True),
            patch.object(sys, "argv", argv),
        ):
            services_mod.main()

        return captured_calls

    def test_use_prebuilt_argument_is_accepted(self):
        """--use-prebuilt is a valid argument that doesn't cause a parse error."""
        services_mod = _import_services()
        # Drive it through argparse without executing side effects
        with (
            patch.object(services_mod, "start_services"),
            patch.object(services_mod, "check_service_configured", return_value=True),
            patch.object(services_mod, "ensure_docker_network", return_value=True),
            patch.object(
                services_mod, "_langfuse_enabled_in_backend", return_value=False
            ),
            patch.dict(os.environ, {}, clear=False),
            patch.object(
                sys,
                "argv",
                ["services.py", "start", "backend", "--use-prebuilt", "v1.0.0"],
            ),
        ):
            # Should not raise
            services_mod.main()

    def test_use_prebuilt_defaults_to_ghcr(self):
        """Without DOCKERHUB_USERNAME or CHRONICLE_REGISTRY, defaults to GHCR."""
        calls = self._run_start(
            ["services.py", "start", "--all", "--use-prebuilt", "v1.0.0"],
            env_override={},
        )
        assert calls.get("CHRONICLE_REGISTRY") == "ghcr.io/simpleopensoftware/"

    def test_use_prebuilt_dockerhub_username_backward_compat(self):
        """DOCKERHUB_USERNAME still works as fallback for --use-prebuilt."""
        calls = self._run_start(
            ["services.py", "start", "--all", "--use-prebuilt", "v1.0.0"],
            env_override={"DOCKERHUB_USERNAME": "myuser"},
        )
        assert calls.get("CHRONICLE_REGISTRY") == "myuser/"

    def test_use_prebuilt_chronicle_registry_takes_precedence(self):
        """CHRONICLE_REGISTRY overrides both DOCKERHUB_USERNAME and the default."""
        calls = self._run_start(
            ["services.py", "start", "--all", "--use-prebuilt", "v1.0.0"],
            env_override={
                "CHRONICLE_REGISTRY": "ghcr.io/custom/",
                "DOCKERHUB_USERNAME": "myuser",
            },
        )
        assert calls.get("CHRONICLE_REGISTRY") == "ghcr.io/custom/"

    def test_use_prebuilt_sets_chronicle_tag_env_var(self):
        """CHRONICLE_TAG is set to the supplied tag when --use-prebuilt is used."""
        calls = self._run_start(
            ["services.py", "start", "--all", "--use-prebuilt", "v2.3.4"],
            env_override={},
        )
        assert calls.get("CHRONICLE_TAG") == "v2.3.4"

    def test_use_prebuilt_disables_build_flag(self):
        """start_services is called with build=False when --use-prebuilt is used."""
        calls = self._run_start(
            ["services.py", "start", "--all", "--use-prebuilt", "v1.0.0"],
            env_override={},
        )
        assert calls.get("build") is False

    def test_build_flag_still_works_without_use_prebuilt(self):
        """--build flag still passes build=True to start_services when --use-prebuilt is absent."""
        calls = self._run_start(
            ["services.py", "start", "--all", "--build"],
            env_override={},
        )
        assert calls.get("build") is True

    def test_normal_start_without_prebuilt_does_not_set_chronicle_vars(self):
        """CHRONICLE_REGISTRY and CHRONICLE_TAG are NOT set for a normal start."""
        env = {**os.environ}
        env.pop("CHRONICLE_REGISTRY", None)
        env.pop("CHRONICLE_TAG", None)

        with patch.dict(os.environ, env, clear=True):
            self._run_start(["services.py", "start", "--all"])
            assert "CHRONICLE_REGISTRY" not in os.environ
            assert "CHRONICLE_TAG" not in os.environ


# ===========================================================================
# 2. docker-compose YAML validation
# ===========================================================================


def _load_compose(relative_path: str) -> dict:
    path = REPO_ROOT / relative_path
    with open(path) as f:
        return yaml.safe_load(f)


def _image_for(compose: dict, service: str) -> str | None:
    return compose.get("services", {}).get(service, {}).get("image")


def _has_chronicle_vars(image_str: str | None) -> bool:
    """True when the image field uses both CHRONICLE_REGISTRY and CHRONICLE_TAG."""
    if image_str is None:
        return False
    return "CHRONICLE_REGISTRY" in image_str and "CHRONICLE_TAG" in image_str


class TestBackendDockerComposeImages:
    COMPOSE = _load_compose("backends/advanced/docker-compose.yml")

    def test_chronicle_backend_has_image_field(self):
        assert _has_chronicle_vars(_image_for(self.COMPOSE, "chronicle-backend"))

    def test_workers_has_image_field(self):
        assert _has_chronicle_vars(_image_for(self.COMPOSE, "workers"))

    def test_annotation_cron_has_image_field(self):
        assert _has_chronicle_vars(_image_for(self.COMPOSE, "annotation-cron"))

    def test_webui_has_image_field(self):
        assert _has_chronicle_vars(_image_for(self.COMPOSE, "webui"))

    def test_backend_services_share_same_image_name(self):
        """chronicle-backend, workers, and annotation-cron should use the same image."""
        backend_img = _image_for(self.COMPOSE, "chronicle-backend")
        workers_img = _image_for(self.COMPOSE, "workers")
        cron_img = _image_for(self.COMPOSE, "annotation-cron")
        assert backend_img == workers_img == cron_img

    def test_webui_uses_different_image_from_backend(self):
        backend_img = _image_for(self.COMPOSE, "chronicle-backend")
        webui_img = _image_for(self.COMPOSE, "webui")
        assert backend_img != webui_img

    def test_image_names_with_defaults_are_local(self):
        """With empty env vars the image names should have no registry prefix."""
        for service in ("chronicle-backend", "webui"):
            image = _image_for(self.COMPOSE, service)
            # The default expansion of ${CHRONICLE_REGISTRY:-} is ""
            # so the name should start with "chronicle-"
            assert image is not None
            assert "chronicle-" in image


class TestSpeakerRecognitionDockerComposeImages:
    COMPOSE = _load_compose("extras/speaker-recognition/docker-compose.yml")

    def test_speaker_service_has_chronicle_image(self):
        assert _has_chronicle_vars(_image_for(self.COMPOSE, "speaker-service"))

    def test_web_ui_has_chronicle_image(self):
        assert _has_chronicle_vars(_image_for(self.COMPOSE, "web-ui"))

    def test_caddy_image_is_not_changed(self):
        """Third-party caddy image must stay unchanged (no chronicle vars)."""
        caddy_img = _image_for(self.COMPOSE, "caddy")
        assert caddy_img is not None
        assert "CHRONICLE_REGISTRY" not in (caddy_img or "")


class TestAsrServicesDockerComposeImages:
    COMPOSE = _load_compose("extras/asr-services/docker-compose.yml")

    @pytest.mark.parametrize(
        "service",
        [
            "nemo-asr",
            "faster-whisper-asr",
            "vibevoice-asr",
            "transformers-asr",
            "qwen3-asr-wrapper",
            "qwen3-asr-bridge",
        ],
    )
    def test_asr_service_has_chronicle_image(self, service):
        assert _has_chronicle_vars(
            _image_for(self.COMPOSE, service)
        ), f"Service '{service}' is missing CHRONICLE_REGISTRY/CHRONICLE_TAG in image: field"

    def test_all_asr_images_are_distinct(self):
        """Each ASR service must resolve to a different image name."""
        services = [
            "nemo-asr",
            "faster-whisper-asr",
            "vibevoice-asr",
            "transformers-asr",
            "qwen3-asr-wrapper",
            "qwen3-asr-bridge",
        ]
        images = [_image_for(self.COMPOSE, s) for s in services]
        assert len(images) == len(
            set(images)
        ), "ASR service image names must all be unique"


class TestHavpeRelayDockerComposeImages:
    COMPOSE = _load_compose("extras/havpe-relay/docker-compose.yml")

    def test_havpe_relay_has_chronicle_image(self):
        assert _has_chronicle_vars(_image_for(self.COMPOSE, "havpe-relay"))


# ===========================================================================
# 3. Bash script input validation
# ===========================================================================


class TestPushScriptValidation:
    """push-images.sh must reject missing inputs without running docker."""

    SCRIPT = SCRIPTS_DIR / "push-images.sh"

    def _run(self, args: list[str], env_override: dict | None = None):
        env = {**os.environ, **(env_override or {})}
        env.pop("DOCKERHUB_USERNAME", None)  # start clean
        env.pop("CHRONICLE_PUSH_REGISTRY", None)  # start clean
        if env_override:
            env.update(env_override)
        return subprocess.run(
            ["bash", str(self.SCRIPT)] + args,
            env=env,
            capture_output=True,
            text=True,
        )

    def test_exits_nonzero_without_any_registry_env(self):
        result = self._run(["v1.0.0"])
        assert result.returncode != 0

    def test_error_message_mentions_registry_options(self):
        result = self._run(["v1.0.0"])
        assert (
            "CHRONICLE_PUSH_REGISTRY" in result.stderr
            or "DOCKERHUB_USERNAME" in result.stderr
        )

    def test_exits_nonzero_without_tag(self):
        result = self._run([], env_override={"DOCKERHUB_USERNAME": "testuser"})
        assert result.returncode != 0

    def test_error_message_mentions_tag_when_tag_missing(self):
        result = self._run([], env_override={"DOCKERHUB_USERNAME": "testuser"})
        assert "TAG" in result.stderr

    def test_script_is_executable(self):
        assert os.access(self.SCRIPT, os.X_OK), "push-images.sh must be executable"


class TestPullScriptValidation:
    """pull-images.sh defaults to GHCR and rejects missing TAG."""

    SCRIPT = SCRIPTS_DIR / "pull-images.sh"

    def _run(self, args: list[str], env_override: dict | None = None):
        env = {**os.environ, **(env_override or {})}
        env.pop("DOCKERHUB_USERNAME", None)
        env.pop("CHRONICLE_REGISTRY", None)
        if env_override:
            env.update(env_override)
        return subprocess.run(
            ["bash", str(self.SCRIPT)] + args,
            env=env,
            capture_output=True,
            text=True,
        )

    def test_exits_nonzero_without_tag(self):
        result = self._run([])
        assert result.returncode != 0

    def test_error_message_mentions_tag_when_tag_missing(self):
        result = self._run([])
        assert "TAG" in result.stderr

    def test_defaults_to_ghcr_without_env_vars(self):
        """pull-images.sh should NOT error when no DOCKERHUB_USERNAME is set (GHCR default)."""
        # We can't actually pull, but we can verify the script doesn't exit
        # at the validation stage. It will fail at docker pull which is fine.
        result = self._run(["v1.0.0"])
        # Should not fail at the input validation stage (returncode 1 with our error message)
        # It will fail later at docker pull, but the stderr should not contain our validation errors
        assert "DOCKERHUB_USERNAME" not in result.stderr
        assert "env var is required" not in result.stderr

    def test_script_is_executable(self):
        assert os.access(self.SCRIPT, os.X_OK), "pull-images.sh must be executable"
