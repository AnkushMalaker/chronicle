import io
import logging
import tempfile
from typing import List

from googleapiclient.http import MediaIoBaseDownload
from starlette.datastructures import UploadFile as StarletteUploadFile

from advanced_omi_backend.clients.gdrive_audio_client import get_google_drive_client
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.utils.audio_utils import AudioValidationError

logger = logging.getLogger(__name__)
audio_logger = logging.getLogger("audio_processing")

AUDIO_EXTENSIONS = (".wav", ".mp3", ".flac", ".ogg", ".m4a")
FOLDER_MIMETYPE = "application/vnd.google-apps.folder"


async def download_and_wrap_drive_file(service, file_item):
    file_id = file_item["id"]
    name = file_item["name"]

    request = service.files().get_media(fileId=file_id)

    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)

    done = False
    while not done:
        _status, done = downloader.next_chunk()

    content = fh.getvalue()

    if not content:
        raise AudioValidationError(f"Downloaded Google Drive file '{name}' was empty")

    tmp_file = tempfile.SpooledTemporaryFile(max_size=10 * 1024 * 1024)  # 10 MB
    tmp_file.write(content)
    tmp_file.seek(0)
    upload_file = StarletteUploadFile(filename=name, file=tmp_file)

    original_close = upload_file.close

    def wrapped_close():
        try:
            original_close()
        finally:
            # SpooledTemporaryFile auto-cleans when closed; no unlink needed
            pass

    upload_file.close = wrapped_close

    return upload_file


# -------------------------------------------------------------
# LIST + DOWNLOAD FILES IN FOLDER (OAUTH)
# -------------------------------------------------------------
async def download_audio_files_from_drive(
    folder_id: str, user_id: str
) -> List[StarletteUploadFile]:
    if not folder_id:
        raise AudioValidationError("Google Drive folder ID is required.")

    service = get_google_drive_client()

    try:
        escaped_folder_id = folder_id.replace("\\", "\\\\").replace("'", "\\'")
        query = f"'{escaped_folder_id}' in parents and trashed = false"

        response = (
            service.files()
            .list(
                q=query,
                fields="files(id, name, mimeType)",
                includeItemsFromAllDrives=False,
                supportsAllDrives=False,
            )
            .execute()
        )

        all_files = response.get("files", [])

        audio_files_metadata = [
            f for f in all_files if f["name"].lower().endswith(AUDIO_EXTENSIONS)
        ]

        if not audio_files_metadata:
            raise AudioValidationError("No audio files found in folder.")

        wrapped_files = []
        skipped_count = 0

        for item in audio_files_metadata:
            file_id = item["id"]  # Get the Google Drive File ID

            # Check if the file is already processed (check Conversation by external_source_id and user_id)
            existing = await Conversation.find_one(
                Conversation.external_source_id == file_id,
                Conversation.external_source_type == "gdrive",
                Conversation.user_id == user_id,
            )

            if existing:
                audio_logger.info(f"Skipping already processed file: {item['name']}")
                skipped_count += 1
                continue

            # synchronous call now (but make the parent function async)
            wrapped_file = await download_and_wrap_drive_file(service, item)
            #  Attach the file_id to the UploadFile object for later use (for external_source_id)
            wrapped_file.file_id = file_id
            wrapped_files.append(wrapped_file)

        if not wrapped_files and skipped_count > 0:
            raise AudioValidationError(
                f"All {skipped_count} files in the folder have already been processed."
            )

        return wrapped_files

    except Exception as e:
        if isinstance(e, AudioValidationError):
            raise
        raise AudioValidationError(f"Google Drive API Error: {e}") from e
