"""Unified speaker database combining SQLite metadata with FAISS performance."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple, cast

import faiss
import numpy as np
from simple_speaker_recognition.database import get_db_session
from simple_speaker_recognition.database.models import Speaker, User
from simple_speaker_recognition.database.queries import UserQueries

log = logging.getLogger(__name__)


def _normalize(arr: np.ndarray) -> np.ndarray:
    """Normalize array to unit length."""
    return arr / np.linalg.norm(arr, axis=-1, keepdims=True)


class UnifiedSpeakerDB:
    """Unified speaker database combining SQLite metadata with FAISS performance."""

    def __init__(self, emb_dim: int, base_dir: Path, similarity_thr: float):
        self._lock = asyncio.Lock()
        self.emb_dim = emb_dim
        self.similarity_thr = similarity_thr
        self.base_dir = base_dir
        self.index_path = base_dir / "faiss.index"

        # FAISS index for fast similarity search using cosine similarity (inner product)
        self.index: faiss.IndexFlatIP = faiss.IndexFlatIP(emb_dim)

        # Mapping from FAISS index position to (user_id, speaker_id)
        self.faiss_to_speaker: Dict[int, Tuple[int, str]] = {}

        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._load_state()

    def _load_state(self) -> None:
        """Load FAISS index and rebuild mapping from SQLite."""
        if self.index_path.exists():
            try:
                self.index = faiss.read_index(str(self.index_path))
                log.info("Loaded FAISS index from %s", self.index_path)
            except Exception as e:
                log.warning("Could not load FAISS index, creating new: %s", e)
                self.index = faiss.IndexFlatIP(self.emb_dim)

        # Rebuild mapping from SQLite
        self._rebuild_faiss_mapping()

    def _rebuild_faiss_mapping(self) -> None:
        """Rebuild FAISS index from SQLite data."""
        db = get_db_session()
        try:
            speakers = db.query(Speaker).all()
            self.faiss_to_speaker.clear()

            if not speakers:
                log.info("No speakers found in database")
                return

            # Recreate FAISS index for inner product (cosine similarity)
            self.index = faiss.IndexFlatIP(self.emb_dim)

            vectors = []
            for i, speaker in enumerate(speakers):
                embedding_data = cast(Optional[str], speaker.embedding_data)
                if embedding_data:
                    try:
                        embedding = np.array(
                            json.loads(embedding_data), dtype=np.float32
                        )
                        vectors.append(embedding)
                        self.faiss_to_speaker[i] = (
                            cast(int, speaker.user_id),
                            cast(str, speaker.id),
                        )
                    except (json.JSONDecodeError, ValueError) as e:
                        log.warning(
                            "Invalid embedding data for speaker %s: %s", speaker.id, e
                        )

            if vectors:
                # Normalize all embeddings before adding to FAISS
                normalized_vectors = np.stack([_normalize(v) for v in vectors]).astype(
                    np.float32
                )
                self.index.add(normalized_vectors)
                log.info(
                    "Rebuilt FAISS index with %d speakers (normalized embeddings)",
                    len(vectors),
                )

        except Exception as e:
            log.error("Error rebuilding FAISS mapping: %s", e)
        finally:
            db.close()

    def _save_faiss_index(self) -> None:
        """Save FAISS index to disk."""
        try:
            faiss.write_index(self.index, str(self.index_path))
        except Exception as e:
            log.error("Error saving FAISS index: %s", e)

    async def add_speaker(
        self,
        speaker_id: str,
        name: str,
        embedding: np.ndarray,
        user_id: int,
        sample_count: int = 1,
        total_duration: float = 0.0,
    ) -> bool:
        """Add speaker with user association; return True if updated (False if new)."""
        async with self._lock:
            db = get_db_session()
            try:
                # Check if speaker exists
                existing_speaker = (
                    db.query(Speaker)
                    .filter(Speaker.id == speaker_id, Speaker.user_id == user_id)
                    .first()
                )

                is_update = existing_speaker is not None

                # Prepare embedding data
                embedding_json = json.dumps(embedding.tolist())

                if is_update:
                    # Update existing speaker (replace enrollment)
                    existing_speaker.name = name  # type: ignore[assignment]
                    existing_speaker.embedding_data = embedding_json  # type: ignore[assignment]
                    existing_speaker.audio_sample_count = sample_count  # type: ignore[assignment]
                    existing_speaker.total_audio_duration = total_duration  # type: ignore[assignment]
                    log.info(
                        "Updated existing speaker: %s (user: %d) with %d samples",
                        speaker_id,
                        user_id,
                        sample_count,
                    )

                    # For updates, we need to rebuild since FAISS doesn't support updates
                    db.commit()
                    self._rebuild_faiss_mapping()
                    self._save_faiss_index()
                else:
                    # Create new speaker
                    new_speaker = Speaker(
                        id=speaker_id,
                        name=name,
                        user_id=user_id,
                        embedding_data=embedding_json,
                        audio_sample_count=sample_count,
                        total_audio_duration=total_duration,
                    )
                    db.add(new_speaker)
                    db.commit()
                    log.info(
                        "Added new speaker: %s (user: %d) with %d samples",
                        speaker_id,
                        user_id,
                        sample_count,
                    )

                    # For new speakers, add to FAISS index incrementally
                    normalized_embedding = _normalize(
                        embedding.astype(np.float32)
                    ).reshape(1, -1)
                    self.index.add(normalized_embedding)

                    # Update mapping (new index position is ntotal - 1)
                    new_faiss_idx = self.index.ntotal - 1
                    self.faiss_to_speaker[new_faiss_idx] = (user_id, speaker_id)

                    self._save_faiss_index()

                return is_update

            except Exception as e:
                db.rollback()
                log.error("Error adding speaker %s: %s", speaker_id, e)
                raise
            finally:
                db.close()

    async def delete_speaker(self, speaker_id: str, user_id: int) -> None:
        """Delete a speaker from the database."""
        async with self._lock:
            db = get_db_session()
            try:
                speaker = (
                    db.query(Speaker)
                    .filter(Speaker.id == speaker_id, Speaker.user_id == user_id)
                    .first()
                )

                if not speaker:
                    raise KeyError(f"Speaker {speaker_id} not found for user {user_id}")

                db.delete(speaker)
                db.commit()

                # Rebuild FAISS index
                self._rebuild_faiss_mapping()
                self._save_faiss_index()

                log.info("Deleted speaker: %s (user: %d)", speaker_id, user_id)

            except Exception as e:
                db.rollback()
                log.error("Error deleting speaker %s: %s", speaker_id, e)
                raise
            finally:
                db.close()

    async def reset_user(self, user_id: int) -> None:
        """Clear all speakers for a specific user."""
        async with self._lock:
            db = get_db_session()
            try:
                db.query(Speaker).filter(Speaker.user_id == user_id).delete()
                db.commit()

                # Rebuild FAISS index
                self._rebuild_faiss_mapping()
                self._save_faiss_index()

                log.info("Reset all speakers for user: %d", user_id)

            except Exception as e:
                db.rollback()
                log.error("Error resetting speakers for user %d: %s", user_id, e)
                raise
            finally:
                db.close()

    async def _rank_candidates(
        self, embedding: np.ndarray, user_id: Optional[int] = None
    ) -> List[Dict]:
        """Return gallery candidates for an embedding, sorted by descending similarity.

        Each candidate is {id, name, user_id, similarity, distance}. Empty list if the
        index is empty or nothing matches the user filter.
        """
        if self.index.ntotal == 0:
            return []

        # Normalize query embedding
        query_emb = _normalize(embedding.astype(np.float32))

        # Use FAISS to find nearest neighbors
        k = min(10, self.index.ntotal)  # Search top-k candidates
        similarities, indices = self.index.search(query_emb.reshape(1, -1), k)

        db = get_db_session()
        try:
            all_candidates: List[Dict] = []
            for idx, similarity in zip(indices[0], similarities[0]):
                if idx == -1:  # FAISS returns -1 for invalid indices
                    continue
                if idx not in self.faiss_to_speaker:
                    continue

                candidate_user_id, speaker_id = self.faiss_to_speaker[idx]

                # Apply user filter if specified
                if user_id is not None and candidate_user_id != user_id:
                    continue

                # FAISS IndexFlatIP returns inner product for normalized vectors (cosine)
                cosine_similarity = float(similarity)

                speaker = (
                    db.query(Speaker)
                    .filter(
                        Speaker.id == speaker_id, Speaker.user_id == candidate_user_id
                    )
                    .first()
                )
                if speaker:
                    all_candidates.append(
                        {
                            "id": speaker.id,
                            "name": speaker.name,
                            "user_id": speaker.user_id,
                            "similarity": cosine_similarity,
                            "distance": 1.0 - cosine_similarity,
                        }
                    )

            ranked = sorted(all_candidates, key=lambda c: c["similarity"], reverse=True)

            if ranked:
                log.info("Speaker identification candidates:")
                for candidate in ranked:
                    log.info(
                        f"  {candidate['name']} ({candidate['id']}): "
                        f"similarity={candidate['similarity']:.4f}, "
                        f"distance={candidate['distance']:.4f}"
                    )
                log.info(f"Threshold: {self.similarity_thr:.4f}")
            else:
                log.info("No valid candidates found for identification")

            return ranked
        except Exception as e:
            log.error("Error during identification: %s", e)
            return []
        finally:
            db.close()

    async def identify(
        self, embedding: np.ndarray, user_id: Optional[int] = None
    ) -> Tuple[bool, Optional[Dict], float]:
        """Identify speaker from embedding using FAISS search (best match only)."""
        found, speaker, similarity, _ = await self.identify_with_candidates(
            embedding, user_id=user_id
        )
        return found, speaker, similarity

    async def identify_with_candidates(
        self, embedding: np.ndarray, user_id: Optional[int] = None
    ) -> Tuple[bool, Optional[Dict], float, List[Dict]]:
        """Like :meth:`identify` but also returns the ranked candidate list.

        Lets callers apply an open-set margin (best vs. runner-up) and exclusive
        assignment across multiple diarized speakers.
        """
        ranked = await self._rank_candidates(embedding, user_id=user_id)
        if not ranked:
            return False, None, 0.0, []

        best = ranked[0]
        best_similarity = best["similarity"]
        if best_similarity >= self.similarity_thr:
            best_speaker = {
                "id": best["id"],
                "name": best["name"],
                "user_id": best["user_id"],
            }
            log.info(
                "Identified speaker: %s (similarity: %.4f)",
                best["name"],
                best_similarity,
            )
            return True, best_speaker, best_similarity, ranked

        log.info("No speaker identified (best similarity: %.4f)", best_similarity)
        return False, None, best_similarity, ranked

    async def verify(
        self, speaker_id: str, embedding: np.ndarray, user_id: int
    ) -> float:
        """Verify speaker identity against stored embedding."""
        db = get_db_session()
        try:
            speaker = (
                db.query(Speaker)
                .filter(Speaker.id == speaker_id, Speaker.user_id == user_id)
                .first()
            )

            embedding_data = (
                cast(Optional[str], speaker.embedding_data) if speaker else None
            )
            if not speaker or not embedding_data:
                raise KeyError(f"Speaker {speaker_id} not enrolled for user {user_id}")

            stored_emb = np.array(json.loads(embedding_data), dtype=np.float32)
            return float(
                np.dot(_normalize(embedding.flatten()), _normalize(stored_emb))
            )

        except Exception as e:
            log.error("Error during verification: %s", e)
            raise
        finally:
            db.close()

    def get_speakers_for_user(self, user_id: int) -> List[Dict]:
        """Get all speakers for a specific user."""
        db = get_db_session()
        try:
            speakers = db.query(Speaker).filter(Speaker.user_id == user_id).all()
            return [
                {
                    "id": cast(str, speaker.id),
                    "name": cast(str, speaker.name),
                    "user_id": cast(int, speaker.user_id),
                    "created_at": speaker.created_at,
                    "updated_at": speaker.updated_at,
                    "audio_sample_count": cast(
                        Optional[int], speaker.audio_sample_count
                    )
                    or 0,
                    "total_audio_duration": cast(
                        Optional[float], speaker.total_audio_duration
                    )
                    or 0.0,
                }
                for speaker in speakers
            ]
        finally:
            db.close()

    def get_speakers_with_embeddings(self, user_id: int) -> Dict[str, Dict]:
        """Get all speakers with their embeddings for a specific user."""
        db = get_db_session()
        try:
            speakers = db.query(Speaker).filter(Speaker.user_id == user_id).all()
            result = {}
            for speaker in speakers:
                embedding_data = cast(Optional[str], speaker.embedding_data)
                if embedding_data:
                    try:
                        embedding = json.loads(embedding_data)
                        result[cast(str, speaker.id)] = {
                            "name": cast(str, speaker.name),
                            "embedding": embedding,
                        }
                    except (json.JSONDecodeError, ValueError) as e:
                        log.warning(
                            "Invalid embedding for speaker %s: %s", speaker.id, e
                        )
            return result
        finally:
            db.close()

    def get_speaker_count(self) -> int:
        """Get total number of enrolled speakers."""
        db = get_db_session()
        try:
            return db.query(Speaker).count()
        finally:
            db.close()

    def ensure_admin_user(self) -> int:
        """Ensure admin user exists and return user ID."""
        db = get_db_session()
        try:
            admin_user = UserQueries.get_or_create_user(db, "admin")
            return cast(int, admin_user.id)
        finally:
            db.close()
