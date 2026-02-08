"""
Logging utilities for secure logging with secret masking.

Provides functions to mask sensitive information in logs to prevent
accidental exposure of credentials, tokens, and other secrets.
"""
import re
from typing import Any, Dict, List, Optional, Set, Union

# Common patterns for identifying secret field names
SECRET_KEYWORDS = [
    'PASSWORD', 'PASSWD', 'PWD',
    'TOKEN', 'AUTH', 'AUTHORIZATION',
    'KEY', 'APIKEY', 'API_KEY', 'SECRET',
    'CREDENTIAL', 'CRED',
    'PRIVATE', 'CERTIFICATE', 'CERT'
]

# Default mask for secrets
SECRET_MASK = '••••••••'


def is_secret_field(field_name: str, additional_keywords: Optional[List[str]] = None) -> bool:
    """
    Check if a field name indicates a secret value.
    
    Args:
        field_name: The field/key name to check
        additional_keywords: Optional additional keywords to check for
        
    Returns:
        True if field name matches secret patterns
        
    Examples:
        >>> is_secret_field('smtp_password')
        True
        >>> is_secret_field('smtp_host')
        False
        >>> is_secret_field('api_token')
        True
    """
    field_upper = field_name.upper()
    
    # Check default keywords
    for keyword in SECRET_KEYWORDS:
        if keyword in field_upper:
            return True
    
    # Check additional keywords if provided
    if additional_keywords:
        for keyword in additional_keywords:
            if keyword.upper() in field_upper:
                return True
    
    return False


def mask_dict(
    data: Dict[str, Any],
    mask: str = SECRET_MASK,
    secret_fields: Optional[Set[str]] = None,
    additional_keywords: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    Mask secret values in a dictionary for safe logging.
    
    Args:
        data: Dictionary to mask
        mask: String to use for masked values (default: '••••••••')
        secret_fields: Explicit set of field names to mask (case-insensitive)
        additional_keywords: Additional keywords to identify secret fields
        
    Returns:
        New dictionary with secrets masked
        
    Examples:
        >>> config = {'smtp_host': 'smtp.gmail.com', 'smtp_password': 'secret123'}
        >>> mask_dict(config)
        {'smtp_host': 'smtp.gmail.com', 'smtp_password': '••••••••'}
        
        >>> mask_dict({'token': 'abc123'}, secret_fields={'token'})
        {'token': '••••••••'}
    """
    masked = {}
    secret_fields_lower = {f.lower() for f in (secret_fields or set())}
    
    for key, value in data.items():
        # Check if this is a secret field
        is_secret = (
            key.lower() in secret_fields_lower or
            is_secret_field(key, additional_keywords)
        )
        
        if is_secret and value:
            # Mask non-empty secret values
            masked[key] = mask
        elif isinstance(value, dict):
            # Recursively mask nested dictionaries
            masked[key] = mask_dict(value, mask, secret_fields, additional_keywords)
        elif isinstance(value, list):
            # Handle lists of dictionaries
            masked[key] = [
                mask_dict(item, mask, secret_fields, additional_keywords)
                if isinstance(item, dict) else item
                for item in value
            ]
        else:
            # Keep non-secret values as-is
            masked[key] = value
    
    return masked


def mask_string(
    text: str,
    patterns: Optional[List[str]] = None,
    mask: str = SECRET_MASK
) -> str:
    """
    Mask sensitive patterns in strings (e.g., tokens in error messages).
    
    Args:
        text: String to mask
        patterns: List of regex patterns to match and mask
        mask: String to use for masked values
        
    Returns:
        String with matched patterns masked
        
    Examples:
        >>> mask_string('Token: abc123def456', patterns=[r'Token: \w+'])
        'Token: ••••••••'
        
        >>> mask_string('password=secret123', patterns=[r'password=\S+'])
        'password=••••••••'
    """
    if not patterns:
        # Default patterns for common secret formats
        patterns = [
            r'password[=:]\s*\S+',
            r'token[=:]\s*\S+',
            r'key[=:]\s*\S+',
            r'secret[=:]\s*\S+',
            r'api[_-]?key[=:]\s*\S+',
        ]
    
    masked_text = text
    for pattern in patterns:
        # Replace the value part after the = or : with mask
        masked_text = re.sub(
            pattern,
            lambda m: re.sub(r'([=:])\s*\S+', r'\1' + mask, m.group(0)),
            masked_text,
            flags=re.IGNORECASE
        )
    
    return masked_text


def safe_log_config(
    config: Dict[str, Any],
    name: str = "Configuration",
    mask: str = SECRET_MASK,
    secret_fields: Optional[Set[str]] = None,
    additional_keywords: Optional[List[str]] = None
) -> str:
    """
    Create a safe log message for configuration with masked secrets.
    
    Args:
        config: Configuration dictionary
        name: Name for the configuration (e.g., "SMTP Config")
        mask: String to use for masked values
        secret_fields: Explicit set of field names to mask
        additional_keywords: Additional keywords to identify secret fields
        
    Returns:
        Formatted string safe for logging
        
    Examples:
        >>> config = {'host': 'smtp.gmail.com', 'password': 'secret', 'port': 587}
        >>> safe_log_config(config, "SMTP")
        "SMTP: {'host': 'smtp.gmail.com', 'password': '••••••••', 'port': 587}"
    """
    masked = mask_dict(config, mask, secret_fields, additional_keywords)
    return f"{name}: {masked}"


def mask_connection_string(connection_string: str, mask: str = SECRET_MASK) -> str:
    """
    Mask credentials in connection strings (URLs, DSNs).
    
    Args:
        connection_string: Connection string that may contain credentials
        mask: String to use for masked values
        
    Returns:
        Connection string with credentials masked
        
    Examples:
        >>> mask_connection_string('mongodb://user:pass123@localhost:27017/db')
        'mongodb://user:••••••••@localhost:27017/db'
        
        >>> mask_connection_string('postgresql://admin:secret@db.example.com/mydb')
        'postgresql://admin:••••••••@db.example.com/mydb'
    """
    # Pattern: protocol://username:password@host
    return re.sub(
        r'([a-zA-Z][a-zA-Z0-9+.-]*://[^:]+:)[^@]+(@)',
        r'\1' + mask + r'\2',
        connection_string
    )


def create_masked_repr(
    obj: Any,
    secret_attrs: Set[str],
    mask: str = SECRET_MASK
) -> str:
    """
    Create a string representation of an object with masked secret attributes.
    
    Useful for __repr__ methods in classes that contain secrets.
    
    Args:
        obj: Object to represent
        secret_attrs: Set of attribute names that are secrets
        mask: String to use for masked values
        
    Returns:
        String representation with secrets masked
        
    Examples:
        >>> class Config:
        ...     def __init__(self):
        ...         self.host = 'smtp.gmail.com'
        ...         self.password = 'secret123'
        >>> 
        >>> config = Config()
        >>> create_masked_repr(config, {'password'})
        "Config(host='smtp.gmail.com', password='••••••••')"
    """
    class_name = obj.__class__.__name__
    attrs = []
    
    for key in dir(obj):
        # Skip private/magic attributes and methods
        if key.startswith('_') or callable(getattr(obj, key)):
            continue
        
        value = getattr(obj, key)
        
        # Mask secret attributes
        if key in secret_attrs:
            value_repr = f"'{mask}'"
        else:
            value_repr = repr(value)
        
        attrs.append(f"{key}={value_repr}")
    
    return f"{class_name}({', '.join(attrs)})"
