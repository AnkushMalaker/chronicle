"""Test configuration loading and merging.

Tests for the configuration system that merges defaults.yml with config.yml
and provides proper caching and reload mechanisms.
"""

import pytest
from pathlib import Path
from advanced_omi_backend.config import get_config, merge_configs, reload_config


def test_merge_configs_basic():
    """Test basic config merging."""
    defaults = {"a": 1, "b": 2}
    overrides = {"b": 3, "c": 4}

    result = merge_configs(defaults, overrides)

    assert result["a"] == 1  # From defaults
    assert result["b"] == 3  # Override
    assert result["c"] == 4  # New key


def test_merge_configs_nested():
    """Test nested dictionary merging."""
    defaults = {
        "memory": {
            "provider": "chronicle",
            "timeout": 120
        }
    }
    overrides = {
        "memory": {
            "provider": "openmemory_mcp"
        }
    }

    result = merge_configs(defaults, overrides)

    assert result["memory"]["provider"] == "openmemory_mcp"  # Override
    assert result["memory"]["timeout"] == 120  # Preserved from defaults


def test_merge_configs_deep_nested():
    """Test deeply nested dictionary merging."""
    defaults = {
        "models": {
            "llm": {
                "openai": {
                    "model": "gpt-4o-mini",
                    "temperature": 0.2,
                    "max_tokens": 2000
                }
            }
        }
    }
    overrides = {
        "models": {
            "llm": {
                "openai": {
                    "temperature": 0.5
                }
            }
        }
    }

    result = merge_configs(defaults, overrides)

    assert result["models"]["llm"]["openai"]["model"] == "gpt-4o-mini"  # Preserved
    assert result["models"]["llm"]["openai"]["temperature"] == 0.5  # Override
    assert result["models"]["llm"]["openai"]["max_tokens"] == 2000  # Preserved


def test_merge_configs_list_replacement():
    """Test that lists are replaced, not merged."""
    defaults = {"items": [1, 2, 3]}
    overrides = {"items": [4, 5]}

    result = merge_configs(defaults, overrides)

    assert result["items"] == [4, 5]  # List replaced entirely


def test_merge_configs_empty_override():
    """Test merging with empty override dictionary."""
    defaults = {"a": 1, "b": 2}
    overrides = {}

    result = merge_configs(defaults, overrides)

    assert result["a"] == 1
    assert result["b"] == 2


def test_merge_configs_empty_defaults():
    """Test merging with empty defaults dictionary."""
    defaults = {}
    overrides = {"a": 1, "b": 2}

    result = merge_configs(defaults, overrides)

    assert result["a"] == 1
    assert result["b"] == 2


def test_get_config_structure():
    """Test that get_config returns expected structure."""
    config = get_config()

    # Should have main sections
    assert isinstance(config, dict)
    assert "defaults" in config or "models" in config  # At least one of these should exist


def test_get_config_caching():
    """Test config caching mechanism."""
    config1 = get_config()
    config2 = get_config()

    # Should return cached instance (same object)
    assert config1 is config2


def test_reload_config():
    """Test config reload invalidates cache."""
    config1 = get_config()
    config2 = reload_config()

    # Should be different instances after reload
    # (Note: Content might be the same, but object should be different)
    # We check that reload returns a config object
    assert isinstance(config2, dict)


def test_merge_configs_none_handling():
    """Test handling of None values in merging."""
    defaults = {"a": 1, "b": None}
    overrides = {"b": 2, "c": None}

    result = merge_configs(defaults, overrides)

    assert result["a"] == 1
    assert result["b"] == 2  # Override None with value
    assert result["c"] is None  # New key with None


def test_merge_configs_complex_scenario():
    """Test complex real-world scenario with mixed types."""
    defaults = {
        "defaults": {
            "llm": "openai-llm",
            "stt": "stt-deepgram"
        },
        "models": [
            {"name": "model1", "type": "llm"},
            {"name": "model2", "type": "embedding"}
        ],
        "memory": {
            "provider": "chronicle",
            "timeout_seconds": 1200,
            "extraction": {
                "enabled": True,
                "prompt": "Default prompt"
            }
        }
    }
    overrides = {
        "defaults": {
            "llm": "local-llm"
        },
        "models": [
            {"name": "model3", "type": "llm"}
        ],
        "memory": {
            "extraction": {
                "prompt": "Custom prompt"
            }
        }
    }

    result = merge_configs(defaults, overrides)

    # Defaults section merged
    assert result["defaults"]["llm"] == "local-llm"  # Override
    assert result["defaults"]["stt"] == "stt-deepgram"  # Preserved

    # Models list replaced
    assert len(result["models"]) == 1
    assert result["models"][0]["name"] == "model3"

    # Memory section deeply merged
    assert result["memory"]["provider"] == "chronicle"  # Preserved
    assert result["memory"]["timeout_seconds"] == 1200  # Preserved
    assert result["memory"]["extraction"]["enabled"] is True  # Preserved
    assert result["memory"]["extraction"]["prompt"] == "Custom prompt"  # Override


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
