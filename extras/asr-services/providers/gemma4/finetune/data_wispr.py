"""Wispr dictation FT: shared English verbatim prompt + windowed dataset.

The target is asr_text (Wispr's raw verbatim ASR, treated as ground truth) cut into honest
<=28s windows. English single-speaker dictation, so the prompt asks for a plain verbatim
English transcript (no speaker labels, no Hinglish wording). Train, eval, and the base
baseline MUST all use WISPR_PROMPT.
"""

from data_windowed import WindowedManifestDataset  # noqa: F401  (re-exported)

WISPR_PROMPT = (
    "Transcribe the following speech segment verbatim in English. "
    "Write digits as digits (e.g. 3, not three). "
    "Only output the transcription text itself, with no commentary or explanation."
)
