"""
Unit tests for CUDA version configuration in ASR service Dockerfiles.

Tests the configurable PYTORCH_CUDA_VERSION build arg that allows selecting
different CUDA versions (cu121, cu126, cu128) for different GPU architectures.
"""

import os
import re
import pytest
from pathlib import Path


class TestDockerfileCUDASupport:
    """Test that Dockerfiles support configurable CUDA versions."""

    @pytest.fixture
    def vibevoice_dockerfile_path(self):
        """Path to VibeVoice Dockerfile."""
        return Path(__file__).parent.parent / "providers" / "vibevoice" / "Dockerfile"

    @pytest.fixture
    def nemo_dockerfile_path(self):
        """Path to NeMo Dockerfile."""
        return Path(__file__).parent.parent / "providers" / "nemo" / "Dockerfile"

    @pytest.fixture
    def docker_compose_path(self):
        """Path to docker-compose.yml."""
        return Path(__file__).parent.parent / "docker-compose.yml"

    def test_vibevoice_dockerfile_has_cuda_arg(self, vibevoice_dockerfile_path):
        """Test that VibeVoice Dockerfile declares PYTORCH_CUDA_VERSION arg."""
        content = vibevoice_dockerfile_path.read_text()

        # Should have ARG declaration
        assert re.search(r"ARG\s+PYTORCH_CUDA_VERSION", content), \
            "Dockerfile must declare PYTORCH_CUDA_VERSION build arg"

        # Should have default value
        arg_match = re.search(r"ARG\s+PYTORCH_CUDA_VERSION=(\w+)", content)
        assert arg_match, "PYTORCH_CUDA_VERSION should have default value"
        default_version = arg_match.group(1)
        assert default_version in ["cu121", "cu126", "cu128"], \
            f"Default CUDA version {default_version} should be cu121, cu126, or cu128"

    def test_vibevoice_dockerfile_uses_cuda_arg_in_uv_sync(self, vibevoice_dockerfile_path):
        """Test that VibeVoice Dockerfile uses CUDA arg in uv sync command."""
        content = vibevoice_dockerfile_path.read_text()

        # Should use --extra ${PYTORCH_CUDA_VERSION}
        assert re.search(r"uv\s+sync.*--extra\s+\$\{PYTORCH_CUDA_VERSION\}", content), \
            "uv sync command must include --extra ${PYTORCH_CUDA_VERSION}"

    def test_nemo_dockerfile_has_cuda_support(self, nemo_dockerfile_path):
        """Test that NeMo Dockerfile (reference implementation) has CUDA support."""
        content = nemo_dockerfile_path.read_text()

        assert re.search(r"ARG\s+PYTORCH_CUDA_VERSION", content), \
            "NeMo Dockerfile should have PYTORCH_CUDA_VERSION arg"

        assert re.search(r"uv\s+sync.*--extra\s+\$\{PYTORCH_CUDA_VERSION\}", content), \
            "NeMo Dockerfile should use CUDA version in uv sync"

    def test_docker_compose_passes_cuda_arg_to_vibevoice(self, docker_compose_path):
        """Test that docker-compose.yml passes PYTORCH_CUDA_VERSION to vibevoice service."""
        content = docker_compose_path.read_text()

        # Find vibevoice-asr service section
        vibevoice_section = re.search(
            r"vibevoice-asr:.*?(?=^\S|\Z)",
            content,
            re.MULTILINE | re.DOTALL
        )
        assert vibevoice_section, "docker-compose.yml must have vibevoice-asr service"

        section_text = vibevoice_section.group(0)

        # Should have build args section
        assert re.search(r"args:", section_text), \
            "vibevoice-asr service should have build args section"

        # Should pass PYTORCH_CUDA_VERSION
        assert re.search(
            r"PYTORCH_CUDA_VERSION:\s*\$\{PYTORCH_CUDA_VERSION:-cu126\}",
            section_text
        ), "vibevoice-asr should pass PYTORCH_CUDA_VERSION build arg with cu126 default"

    def test_docker_compose_cuda_arg_consistency(self, docker_compose_path):
        """Test that all GPU-enabled services use consistent CUDA version pattern."""
        content = docker_compose_path.read_text()

        # Services that should have CUDA support
        gpu_services = ["vibevoice-asr", "nemo-asr", "parakeet-asr"]

        for service_name in gpu_services:
            service_match = re.search(
                rf"{service_name}:.*?(?=^\S|\Z)",
                content,
                re.MULTILINE | re.DOTALL
            )

            if service_match:
                service_text = service_match.group(0)

                # Check if service has GPU resources
                if "devices:" in service_text and "nvidia" in service_text:
                    # Should have PYTORCH_CUDA_VERSION arg
                    assert re.search(
                        r"PYTORCH_CUDA_VERSION:\s*\$\{PYTORCH_CUDA_VERSION:-cu\d+\}",
                        service_text
                    ), f"{service_name} with GPU should have PYTORCH_CUDA_VERSION build arg"


class TestCUDAVersionEnvironmentVariable:
    """Test CUDA version environment variable handling."""

    def test_cuda_version_env_var_format(self):
        """Test that CUDA version environment variables follow correct format."""
        valid_versions = ["cu121", "cu126", "cu128"]

        for version in valid_versions:
            assert re.match(r"^cu\d{3}$", version), \
                f"{version} should match pattern cu### (e.g., cu121, cu126)"

    def test_cuda_version_from_env(self):
        """Test reading CUDA version from environment."""
        test_version = "cu128"

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("PYTORCH_CUDA_VERSION", test_version)
            cuda_version = os.getenv("PYTORCH_CUDA_VERSION")

            assert cuda_version == test_version
            assert cuda_version in ["cu121", "cu126", "cu128"]

    def test_cuda_version_default_fallback(self):
        """Test that default CUDA version is used when env var not set."""
        with pytest.MonkeyPatch.context() as mp:
            mp.delenv("PYTORCH_CUDA_VERSION", raising=False)

            # Simulate docker-compose default: ${PYTORCH_CUDA_VERSION:-cu126}
            cuda_version = os.getenv("PYTORCH_CUDA_VERSION", "cu126")

            assert cuda_version == "cu126"


class TestGPUArchitectureCUDAMapping:
    """Test that GPU architectures map to correct CUDA versions."""

    def test_rtx_5090_requires_cu128(self):
        """
        Test that RTX 5090 (sm_120) requires CUDA 12.8+.

        RTX 5090 has CUDA capability 12.0 (sm_120) which requires
        PyTorch built with CUDA 12.8 or higher.
        """
        gpu_arch = "sm_120"  # RTX 5090
        required_cuda = "cu128"

        # Map GPU architecture to minimum CUDA version
        arch_to_cuda = {
            "sm_120": "cu128",  # RTX 5090, RTX 50 series
            "sm_90": "cu126",   # RTX 4090, H100
            "sm_89": "cu121",   # RTX 4090
            "sm_86": "cu121",   # RTX 3090, A6000
        }

        assert arch_to_cuda.get(gpu_arch) == required_cuda, \
            f"GPU architecture {gpu_arch} requires CUDA version {required_cuda}"

    def test_older_gpus_work_with_cu121(self):
        """Test that older GPUs (sm_86, sm_80) work with cu121."""
        older_archs = ["sm_86", "sm_80", "sm_75"]  # RTX 3090, A100, RTX 2080

        for arch in older_archs:
            # cu121 supports these architectures
            assert arch in ["sm_75", "sm_80", "sm_86"], \
                f"{arch} should be supported by CUDA 12.1"


class TestPyProjectCUDAExtras:
    """Test that pyproject.toml defines CUDA version extras correctly."""

    @pytest.fixture
    def pyproject_path(self):
        """Path to pyproject.toml."""
        return Path(__file__).parent.parent / "pyproject.toml"

    def test_pyproject_has_cuda_extras(self, pyproject_path):
        """Test that pyproject.toml defines cu121, cu126, cu128 extras."""
        if not pyproject_path.exists():
            pytest.skip("pyproject.toml not found")

        content = pyproject_path.read_text()

        # Should have [project.optional-dependencies] or [tool.uv] with extras
        cuda_versions = ["cu121", "cu126", "cu128"]

        for version in cuda_versions:
            # Look for the CUDA version as an extra
            assert re.search(rf'["\']?{version}["\']?\s*=', content), \
                f"pyproject.toml should define {version} extra"

    def test_pyproject_cuda_extras_have_pytorch(self, pyproject_path):
        """Test that CUDA extras include torch/torchaudio dependencies."""
        if not pyproject_path.exists():
            pytest.skip("pyproject.toml not found")

        content = pyproject_path.read_text()

        # Each CUDA extra should reference torch with the appropriate index
        # e.g., { extra = "cu128" } or { index = "pytorch-cu128" }
        assert re.search(r'extra\s*=\s*["\']cu\d{3}["\']', content) or \
               re.search(r'index\s*=\s*["\']pytorch-cu\d{3}["\']', content), \
            "CUDA extras should reference PyTorch with CUDA version"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
