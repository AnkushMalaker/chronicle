import os

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from advanced_omi_backend.app_config import get_app_config

_drive_client_cache = None

def get_google_drive_client():
    """Singleton Google Drive client."""
    global _drive_client_cache

    if _drive_client_cache:
        return _drive_client_cache

    config = get_app_config()

    if not os.path.exists(config.gdrive_credentials_path):
        raise FileNotFoundError(
            f"Missing Google Drive credentials at {config.gdrive_credentials_path}"
        )

    creds = Credentials.from_service_account_file(
        config.gdrive_credentials_path,
        scopes=config.gdrive_scopes
    )

    _drive_client_cache = build("drive", "v3", credentials=creds)

    return _drive_client_cache
