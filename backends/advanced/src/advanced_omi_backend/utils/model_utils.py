"""
Model configuration utilities for LLM services.

This module provides reusable utilities for retrieving model configurations
from config.yml that can be used across different LLM services.
"""

from typing import Dict, Any


def get_model_config(config_data: Dict[str, Any], model_role: str) -> Dict[str, Any]:
    """
    Get model configuration for a given role from config.yml data.
    
    This function looks up the default model name for the given role in the
    'defaults' section, then finds the corresponding model definition in the
    'models' section.
    
    Args:
        config_data: Parsed config.yml data (dict with 'defaults' and 'models' keys)
        model_role: The role to look up (e.g., 'llm', 'embedding', 'stt', 'tts')
    
    Returns:
        Model configuration dictionary if found
    
    Raises:
        ValueError: If the default for the role is not found or the model
                   definition is not found in the models list.
    
    Example:
        >>> from advanced_omi_backend.services.memory.config import load_config_yml
        >>> from advanced_omi_backend.utils.model_utils import get_model_config
        >>> config_data = load_config_yml()
        >>> llm_config = get_model_config(config_data, 'llm')
        >>> print(llm_config['model_name'])
    """
    default_name = config_data.get('defaults', {}).get(model_role)
    if not default_name:
        raise ValueError(f"Configuration for 'defaults.{model_role}' not found in config.yml")
    
    for model in config_data.get('models', []):
        if model.get('name') == default_name:
            return model
    
    raise ValueError(f"Model '{default_name}' for role '{model_role}' not found in config.yml models list")

