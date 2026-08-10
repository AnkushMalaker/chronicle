"""Regression tests for local llama.cpp model-registry selection."""

from __future__ import annotations

import importlib.util
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from dotenv import dotenv_values


@pytest.fixture(scope="module")
def init_module():
    path = Path(__file__).resolve().parents[1] / "init.py"
    spec = importlib.util.spec_from_file_location(
        "chronicle_llm_services_init_test", path
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Console:
    def print(self, *_args, **_kwargs):
        return None


class _ConfigManager:
    instances = []
    initial_config = None

    def __init__(self, service_path):
        assert service_path == "extras/llm-services"
        default_config = {
            "defaults": {},
            "models": [
                {
                    "name": "muse-glimmer-llm",
                    "model_type": "llm",
                    "api_family": "openai",
                    "model_name": "stale-model-id",
                }
            ],
        }
        self.config = deepcopy(type(self).initial_config or default_config)
        self.synced = []
        self.updated_defaults = None
        self.saved = None
        type(self).instances.append(self)

    def get_full_config(self):
        return self.config

    def sync_models_from_defaults(self, names):
        self.synced.extend(names)
        canonical = {
            "muse-glimmer-llm": {
                "name": "muse-glimmer-llm",
                "description": "Muse Glimmer",
                "model_type": "llm",
                "model_provider": "llamacpp",
                "api_family": "openai",
                "model_name": "meta-models/Muse-Glimmer-30B-GGUF:muse-glimmer-30B-kquant-17gb.gguf",
                "model_params": {"max_tokens": 2000},
            },
            "llamacpp-llm": {
                "name": "llamacpp-llm",
                "model_type": "llm",
                "api_family": "openai",
                "model_name": "glm-4.7-flash",
            },
            "llamacpp-embed": {
                "name": "llamacpp-embed",
                "model_type": "embedding",
                "api_family": "openai",
                "model_name": "nomic-embed",
            },
        }
        for name in names:
            self.add_or_update_model(deepcopy(canonical[name]))
        return list(names)

    def add_or_update_model(self, model):
        for index, existing in enumerate(self.config["models"]):
            if existing.get("name") == model["name"]:
                self.config["models"][index] = deepcopy(model)
                break
        else:
            self.config["models"].append(deepcopy(model))

    def update_config_defaults(self, defaults):
        self.config["defaults"].update(defaults)
        self.updated_defaults = deepcopy(defaults)

    def save_full_config(self, config):
        self.config = deepcopy(config)
        self.saved = deepcopy(config)


def test_muse_selection_syncs_registry_entry_and_exact_upstream_identity(
    init_module, monkeypatch
):
    _ConfigManager.instances.clear()
    monkeypatch.setattr(init_module, "ConfigManager", _ConfigManager)
    setup = object.__new__(init_module.LLMServicesSetup)
    setup.console = _Console()
    muse_repo = "meta-models/Muse-Glimmer-30B-GGUF"
    muse = init_module.LLM_MODELS[muse_repo]

    setup.update_config_yml(muse_repo, muse)

    manager = _ConfigManager.instances[-1]
    assert manager.synced == ["muse-glimmer-llm", "llamacpp-embed"]
    assert manager.updated_defaults == {
        "llm": "muse-glimmer-llm",
        "embedding": "llamacpp-embed",
    }
    selected = next(
        model
        for model in manager.config["models"]
        if model["name"] == "muse-glimmer-llm"
    )
    assert selected["model_name"] == (
        "meta-models/Muse-Glimmer-30B-GGUF:" "muse-glimmer-30B-kquant-17gb.gguf"
    )
    assert selected["context_window"] == 131072
    assert selected["thinking"] is True


def test_root_wizard_pi_flow_reconciles_to_concrete_muse_contract(
    init_module, monkeypatch
):
    """Backend-first setup is reconciled after the local model is selected."""
    initial = {
        "defaults": {"llm": "llamacpp-llm", "embedding": "llamacpp-embed"},
        "models": [],
        "memory": {
            "agents": {
                "write": {"backend": "pi", "recovery_backend": "direct"},
                "search": {"backend": "pi"},
            },
            "backends": {
                "pi": {
                    "model": "llamacpp-llm",
                    "context_window": 32768,
                    "max_tokens": 4096,
                }
            },
        },
    }
    monkeypatch.setattr(_ConfigManager, "initial_config", initial)
    _ConfigManager.instances.clear()
    monkeypatch.setattr(init_module, "ConfigManager", _ConfigManager)
    setup = object.__new__(init_module.LLMServicesSetup)
    setup.console = _Console()
    muse = init_module.LLM_MODELS[init_module.MUSE_GLIMMER_REPO]

    setup.update_config_yml(init_module.MUSE_GLIMMER_REPO, muse)

    config = _ConfigManager.instances[-1].config
    assert config["defaults"]["llm"] == "muse-glimmer-llm"
    selected = next(
        model for model in config["models"] if model["name"] == "muse-glimmer-llm"
    )
    assert selected["model_name"] == (
        "meta-models/Muse-Glimmer-30B-GGUF:" "muse-glimmer-30B-kquant-17gb.gguf"
    )
    assert selected["context_window"] == 131072
    pi = config["memory"]["backends"]["pi"]
    assert pi == {
        "model": "muse-glimmer-llm",
        "context_window": 131072,
        "max_tokens": 4096,
    }


def test_reconciliation_preserves_explicit_external_pi_model(init_module, monkeypatch):
    initial = {
        "defaults": {"llm": "llamacpp-llm"},
        "models": [],
        "memory": {
            "agents": {
                "write": {"backend": "pi", "recovery_backend": None},
                "search": {"backend": "pi"},
            },
            "backends": {
                "pi": {
                    "model": "external-pi-llm",
                    "context_window": 131072,
                    "max_tokens": 8192,
                }
            },
        },
    }
    monkeypatch.setattr(_ConfigManager, "initial_config", initial)
    _ConfigManager.instances.clear()
    monkeypatch.setattr(init_module, "ConfigManager", _ConfigManager)
    setup = object.__new__(init_module.LLMServicesSetup)
    setup.console = _Console()

    setup.update_config_yml(
        init_module.MUSE_GLIMMER_REPO,
        init_module.LLM_MODELS[init_module.MUSE_GLIMMER_REPO],
    )

    pi = _ConfigManager.instances[-1].config["memory"]["backends"]["pi"]
    assert pi == {
        "model": "external-pi-llm",
        "context_window": 131072,
        "max_tokens": 8192,
    }


def test_non_qwen_selection_keeps_generic_llamacpp_registry_entry(
    init_module, monkeypatch
):
    _ConfigManager.instances.clear()
    monkeypatch.setattr(init_module, "ConfigManager", _ConfigManager)
    setup = object.__new__(init_module.LLMServicesSetup)
    setup.console = _Console()
    repo = "bartowski/zai-org_GLM-4.7-Flash-GGUF"

    setup.update_config_yml(repo, init_module.LLM_MODELS[repo])

    manager = _ConfigManager.instances[-1]
    assert manager.synced == ["llamacpp-llm", "llamacpp-embed"]
    assert manager.updated_defaults["llm"] == "llamacpp-llm"
    selected = next(
        model for model in manager.config["models"] if model["name"] == "llamacpp-llm"
    )
    assert selected["model_name"] == "bartowski/zai-org_GLM-4.7-Flash-GGUF:Q4_K_M"
    assert selected["context_window"] == 8192
    assert selected["thinking"] is True


def test_non_reasoning_selection_updates_generic_registry_contract(
    init_module, monkeypatch
):
    _ConfigManager.instances.clear()
    monkeypatch.setattr(init_module, "ConfigManager", _ConfigManager)
    setup = object.__new__(init_module.LLMServicesSetup)
    setup.console = _Console()
    repo = "bartowski/Meta-Llama-3.1-8B-Instruct-GGUF"

    setup.update_config_yml(repo, init_module.LLM_MODELS[repo])

    manager = _ConfigManager.instances[-1]
    selected = next(
        model for model in manager.config["models"] if model["name"] == "llamacpp-llm"
    )
    assert selected["model_name"] == "bartowski/Meta-Llama-3.1-8B-Instruct-GGUF:Q4_K_M"
    assert selected["context_window"] == 8192
    assert selected["thinking"] is False


def test_custom_local_gguf_uses_file_as_registry_identity(init_module, monkeypatch):
    _ConfigManager.instances.clear()
    monkeypatch.setattr(init_module, "ConfigManager", _ConfigManager)
    setup = object.__new__(init_module.LLMServicesSetup)
    setup.console = _Console()

    setup.update_config_yml(
        None,
        {
            "file": "private-model.gguf",
            "description": "Custom local model",
            "ctx_size": 12288,
            "thinking": False,
        },
    )

    manager = _ConfigManager.instances[-1]
    selected = next(
        model for model in manager.config["models"] if model["name"] == "llamacpp-llm"
    )
    assert selected["model_name"] == "private-model.gguf"
    assert selected["context_window"] == 12288


def test_registry_sync_failure_is_fatal(init_module, monkeypatch):
    class _BrokenConfigManager(_ConfigManager):
        def sync_models_from_defaults(self, names):
            self.synced.extend(names)
            return ["llamacpp-embed"]

    monkeypatch.setattr(init_module, "ConfigManager", _BrokenConfigManager)
    setup = object.__new__(init_module.LLMServicesSetup)
    setup.console = _Console()

    with pytest.raises(ValueError, match="Could not sync required model"):
        setup.update_config_yml(
            init_module.MUSE_GLIMMER_REPO,
            init_module.LLM_MODELS[init_module.MUSE_GLIMMER_REPO],
        )


def test_rerun_defaults_to_current_muse_model(init_module):
    setup = object.__new__(init_module.LLMServicesSetup)
    setup.console = _Console()
    setup.args = SimpleNamespace(llm_model=None, ctx_size=None)
    setup.read_existing_env_value = lambda key: (
        "meta-models/Muse-Glimmer-30B-GGUF" if key == "LLM_HF_REPO" else None
    )
    selected_defaults = []

    def choose(_prompt, _choices, default):
        selected_defaults.append(default)
        return default

    setup.prompt_choice = choose

    repo, info = setup.select_llm_model()

    assert selected_defaults == ["3"]
    assert repo == init_module.MUSE_GLIMMER_REPO
    assert info["ctx_size"] == 131072


def test_noninteractive_model_gpu_and_context_arguments_are_honored(init_module):
    setup = object.__new__(init_module.LLMServicesSetup)
    setup.console = _Console()
    setup.args = SimpleNamespace(
        llm_model="meta-models/Muse-Glimmer-30B-GGUF",
        embed_model="nomic-ai/nomic-embed-text-v1.5-GGUF:Q8_0",
        n_gpu_layers="42",
        ctx_size="49152",
    )
    setup.config = {}
    setup.print_section = lambda _title: None

    llm_repo, llm = setup.select_llm_model()
    embed_repo, embed = setup.select_embed_model()
    setup.setup_gpu_config()

    assert llm_repo == init_module.MUSE_GLIMMER_REPO
    assert llm["ctx_size"] == 49152
    assert embed_repo == "nomic-ai/nomic-embed-text-v1.5-GGUF"
    assert embed["dimensions"] == 768
    assert setup.config["N_GPU_LAYERS"] == "42"


def test_network_contract_defaults_host_publication_to_loopback(init_module):
    setup = object.__new__(init_module.LLMServicesSetup)
    setup.console = _Console()
    setup.args = SimpleNamespace()
    setup.config = {}
    setup.read_existing_env_value = lambda _key: None

    setup.configure_network_contract()

    assert setup.config["LLM_BIND_HOST"] == "127.0.0.1"
    assert setup.config["EMBED_BIND_HOST"] == "127.0.0.1"
    assert "LLAMA_API_KEY" not in setup.config


def test_network_contract_requires_auth_for_non_loopback_bind(init_module):
    setup = object.__new__(init_module.LLMServicesSetup)
    setup.console = _Console()
    setup.args = SimpleNamespace()
    setup.config = {}
    values = {"LLM_BIND_HOST": "0.0.0.0"}
    setup.read_existing_env_value = lambda key: values.get(key)

    with pytest.raises(ValueError, match="requires LLAMA_API_KEY"):
        setup.configure_network_contract()


def test_network_contract_also_protects_embedding_bind(init_module):
    setup = object.__new__(init_module.LLMServicesSetup)
    setup.console = _Console()
    setup.args = SimpleNamespace()
    setup.config = {}
    values = {"EMBED_BIND_HOST": "0.0.0.0"}
    setup.read_existing_env_value = lambda key: values.get(key)

    with pytest.raises(
        ValueError, match="non-loopback embedding bind requires LLAMA_API_KEY"
    ):
        setup.configure_network_contract()


def test_network_contract_rejects_persisted_tailnet_bind(init_module):
    setup = object.__new__(init_module.LLMServicesSetup)
    setup.console = _Console()
    setup.args = SimpleNamespace()
    setup.config = {}
    values = {"LLM_BIND_HOST": "100.64.10.20", "LLAMA_API_KEY": "secret"}
    setup.read_existing_env_value = lambda key: values.get(key)

    with pytest.raises(ValueError, match="Do not persist a Tailscale IP"):
        setup.configure_network_contract()


def test_muse_uses_multimodal_dflash_server_profile(init_module):
    repo = init_module.MUSE_GLIMMER_REPO

    profile = init_module.LLMServicesSetup._server_profile(
        repo, init_module.LLM_MODELS[repo]
    )

    assert profile == {
        "LLAMA_ARG_N_PARALLEL": "1",
        "LLAMA_ARG_FLASH_ATTN": "on",
        "LLAMA_ARG_JINJA": "true",
        "LLAMA_ARG_CACHE_TYPE_K": "q8_0",
        "LLAMA_ARG_CACHE_TYPE_V": "q8_0",
        "LLAMA_ARG_MMPROJ_AUTO": "true",
        "LLAMA_ARG_THINK_BUDGET": "-1",
        "LLAMA_ARG_REASONING": "on",
        "LLAMA_ARG_SPEC_TYPE": "draft-dflash",
        "LLAMA_ARG_SPEC_DRAFT_MODEL": "/cache/dflash-kquant.gguf",
        "LLAMA_ARG_SPEC_DRAFT_N_MAX": "4",
        "LLAMA_ARG_FIT": "off",
        "LLAMA_DRAFT_MODEL_URL": (
            "https://huggingface.co/meta-models/Muse-Glimmer-30B-GGUF/"
            "resolve/main/dflash-kquant.gguf"
        ),
    }


def test_non_qwen_profile_restores_safe_llamacpp_defaults(init_module):
    repo = "bartowski/zai-org_GLM-4.7-Flash-GGUF"

    profile = init_module.LLMServicesSetup._server_profile(
        repo, init_module.LLM_MODELS[repo]
    )

    assert profile == {
        "LLAMA_ARG_N_PARALLEL": "1",
        "LLAMA_ARG_FLASH_ATTN": "auto",
        "LLAMA_ARG_JINJA": "true",
        "LLAMA_ARG_CACHE_TYPE_K": "f16",
        "LLAMA_ARG_CACHE_TYPE_V": "f16",
        "LLAMA_ARG_MMPROJ_AUTO": "true",
        "LLAMA_ARG_THINK_BUDGET": "-1",
        "LLAMA_ARG_REASONING": "auto",
        "LLAMA_ARG_SPEC_TYPE": "none",
        "LLAMA_ARG_SPEC_DRAFT_MODEL": "",
        "LLAMA_ARG_SPEC_DRAFT_N_MAX": "3",
        "LLAMA_ARG_FIT": "on",
        "LLAMA_DRAFT_MODEL_URL": "",
    }


def test_generate_env_file_writes_muse_server_profile(
    init_module, monkeypatch, tmp_path
):
    setup = object.__new__(init_module.LLMServicesSetup)
    setup.console = _Console()
    setup.config = init_module.LLMServicesSetup._server_profile(
        init_module.MUSE_GLIMMER_REPO,
        init_module.LLM_MODELS[init_module.MUSE_GLIMMER_REPO],
    )
    monkeypatch.setattr(init_module, "SERVICE_DIR", tmp_path)
    setup.backup_existing_env = lambda: None

    setup.generate_env_file()

    env = dotenv_values(tmp_path / ".env")
    assert env["LLAMA_ARG_N_PARALLEL"] == "1"
    assert env["LLAMA_ARG_FLASH_ATTN"] == "on"
    assert env["LLAMA_ARG_JINJA"] == "true"
    assert env["LLAMA_ARG_CACHE_TYPE_K"] == "q8_0"
    assert env["LLAMA_ARG_CACHE_TYPE_V"] == "q8_0"
    assert env["LLAMA_ARG_MMPROJ_AUTO"] == "true"
    assert env["LLAMA_ARG_REASONING"] == "on"
    assert env["LLAMA_ARG_SPEC_TYPE"] == "draft-dflash"
    assert env["LLAMA_ARG_SPEC_DRAFT_MODEL"] == "/cache/dflash-kquant.gguf"


def test_compose_forwards_server_profile_with_safe_defaults(init_module):
    compose_path = Path(init_module.__file__).resolve().parent / "docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    environment = compose["services"]["llama-cpp-llm"]["environment"]

    assert environment["LLAMA_ARG_N_PARALLEL"] == "${LLAMA_ARG_N_PARALLEL:-1}"
    assert environment["LLAMA_ARG_FLASH_ATTN"] == "${LLAMA_ARG_FLASH_ATTN:-auto}"
    assert environment["LLAMA_ARG_JINJA"] == "${LLAMA_ARG_JINJA:-true}"
    assert environment["LLAMA_ARG_CACHE_TYPE_K"] == "${LLAMA_ARG_CACHE_TYPE_K:-f16}"
    assert environment["LLAMA_ARG_CACHE_TYPE_V"] == "${LLAMA_ARG_CACHE_TYPE_V:-f16}"
    assert environment["LLAMA_ARG_MMPROJ_AUTO"] == "${LLAMA_ARG_MMPROJ_AUTO:-true}"
    assert environment["LLAMA_ARG_REASONING"] == "${LLAMA_ARG_REASONING:-auto}"
    assert environment["LLAMA_ARG_SPEC_TYPE"] == "${LLAMA_ARG_SPEC_TYPE:-none}"
    assert environment["LLAMA_ARG_SPEC_DRAFT_MODEL"] == (
        "${LLAMA_ARG_SPEC_DRAFT_MODEL:-}"
    )
    assert environment["LLAMA_ARG_SPEC_DRAFT_N_MAX"] == (
        "${LLAMA_ARG_SPEC_DRAFT_N_MAX:-3}"
    )
    assert environment["LLAMA_ARG_FIT"] == "${LLAMA_ARG_FIT:-on}"
    assert compose["services"]["llama-cpp-llm"]["image"] == (
        "localhost/chronicle-llama-cpp:muse-glimmer-62bf73d"
    )
    assert compose["services"]["llama-cpp-embed"]["image"] == (
        "ghcr.io/ggml-org/llama.cpp:server-cuda-b10290"
    )
    assert compose["services"]["llama-cpp-llm"]["ports"] == [
        "${LLM_BIND_HOST:-127.0.0.1}:${LLM_PORT:-8083}:8080"
    ]
    assert compose["services"]["llama-cpp-embed"]["ports"] == [
        "${EMBED_BIND_HOST:-127.0.0.1}:${EMBED_PORT:-8082}:8080"
    ]
    assert environment["LLAMA_API_KEY"] == "${LLAMA_API_KEY:-}"
    assert (
        compose["services"]["llama-cpp-embed"]["environment"]["LLAMA_API_KEY"]
        == "${LLAMA_API_KEY:-}"
    )

    defaults_path = (
        Path(init_module.__file__).resolve().parents[2] / "config/defaults.yml"
    )
    defaults = yaml.safe_load(defaults_path.read_text(encoding="utf-8"))
    models = {model["name"]: model for model in defaults["models"]}
    for name in ("llamacpp-llm", "muse-glimmer-llm"):
        assert models[name]["discovery_default"] == "http://llama-cpp-llm:8080/v1"
        assert models[name]["api_key"] == "${oc.env:LLAMA_API_KEY,no-key}"
    assert models["llamacpp-embed"]["model_url"] == ("http://llama-cpp-embed:8080/v1")
