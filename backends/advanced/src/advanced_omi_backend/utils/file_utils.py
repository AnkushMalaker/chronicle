"""
File utility functions for handling file operations like zip extraction.

This module provides reusable utilities for common file operations that may be
needed across different parts of the application.
"""

import logging
import zipfile
from pathlib import Path
from typing import Union

logger = logging.getLogger(__name__)


class ZipExtractionError(Exception):
    """Exception raised when zip extraction fails."""
    pass


def extract_zip(
    zip_path: Union[str, Path],
    extract_dir: Union[str, Path],
    create_extract_dir: bool = True
) -> Path:
    """
    Extract a zip file to a specified directory.

    Args:
        zip_path: Path to the zip file to extract
        extract_dir: Directory where the zip contents should be extracted
        create_extract_dir: Whether to create the extraction directory if it doesn't exist (default: True)

    Returns:
        Path: The extraction directory path

    Raises:
        ZipExtractionError: If extraction fails for any reason
        FileNotFoundError: If the zip file doesn't exist
        zipfile.BadZipFile: If the file is not a valid zip file

    Example:
        >>> from advanced_omi_backend.utils.file_utils import extract_zip
        >>> extract_path = extract_zip("/path/to/archive.zip", "/path/to/extract/to")
        >>> print(f"Extracted to: {extract_path}")
    """
    zip_path = Path(zip_path)
    extract_dir = Path(extract_dir)

    # Validate zip file exists
    if not zip_path.exists():
        error_msg = f"Zip file not found: {zip_path}"
        logger.error(error_msg)
        raise FileNotFoundError(error_msg)

    # Create extraction directory if needed
    if create_extract_dir:
        extract_dir.mkdir(parents=True, exist_ok=True)

    # Extract zip file
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(extract_dir)
        logger.info(f"Successfully extracted {zip_path} to {extract_dir}")
        return extract_dir
    except zipfile.BadZipFile as e:
        error_msg = f"Invalid zip file: {zip_path} - {e}"
        logger.error(error_msg)
        raise zipfile.BadZipFile(error_msg) from e
    except zipfile.LargeZipFile as e:
        error_msg = f"Zip file too large: {zip_path} - {e}"
        logger.error(error_msg)
        raise ZipExtractionError(error_msg) from e
    except PermissionError as e:
        error_msg = f"Permission denied extracting zip file: {zip_path} - {e}"
        logger.error(error_msg)
        raise ZipExtractionError(error_msg) from e
    except Exception as e:
        error_msg = f"Error extracting zip file {zip_path}: {e}"
        logger.error(error_msg)
        raise ZipExtractionError(error_msg) from e

