"""One-time backfill of per-cluster speaker centroids for existing conversations.

Embeds one centroid per diarized speaker from each conversation's already-stored segment
boundaries (no re-diarization) and saves it to
``TranscriptVersion.metadata["cluster_centroids"]``. This lets the "reprocess impact"
finder re-identify past conversations against the current gallery with pure vector math.

GPU-bound (runs the wespeaker embedder via the speaker service). Idempotent: skips
conversations that already have stored centroids unless you flip only_missing.

Run inside the backend container:
    python3 /app/scripts/backfill_cluster_embeddings.py            # all missing
    python3 /app/scripts/backfill_cluster_embeddings.py 5          # first 5 (smoke test)
"""

import asyncio
import sys

from beanie import init_beanie

from advanced_omi_backend.controllers.drift_controller import (
    backfill_cluster_embeddings,
)
from advanced_omi_backend.database import db
from advanced_omi_backend.models.audio_chunk import AudioChunkDocument
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.user import User


async def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    # AudioChunkDocument is needed because backfill reconstructs conversation audio.
    await init_beanie(
        database=db, document_models=[User, Conversation, AudioChunkDocument]
    )
    result = await backfill_cluster_embeddings(limit=limit, only_missing=True)
    print(f"Backfill result: {result}")


if __name__ == "__main__":
    asyncio.run(main())
