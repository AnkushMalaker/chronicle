"""Shared constants for speaker recognition.

Kept dependency-free so it can be imported from any module (including core/)
without triggering heavy or circular imports.
"""

# Minimum cosine similarity (on normalized wespeaker embeddings) required to
# treat a segment as the same / a known speaker. Single source of truth for
# every "similarity threshold" default across the service.
#
# Note: pyannote/wespeaker-voxceleb-resnet34-LM embeddings operate in a
# compressed cosine range (same-speaker scores realistically top out ~0.55-0.6),
# so values >0.6 reject almost everything and values <0.3 match almost anything.
DEFAULT_SIMILARITY_THRESHOLD = 0.5
