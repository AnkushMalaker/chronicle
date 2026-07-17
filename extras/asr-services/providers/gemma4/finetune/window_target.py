"""Proportionally truncate transcripts to the audio window the model actually hears.

CoSHE clips are mostly ~57s but Gemma4's audio encoder caps at 30s, and the dataset has
no word timestamps. So for a coherent "transcribe the first W seconds" task we truncate
each clip's transcript to the first `W/duration` fraction of its characters (≈ the words
spoken in the heard window, assuming roughly uniform speech rate). Clips <= W keep the
full transcript. Applied identically in training and eval so base/FT and ref/hyp stay
consistent.
"""

import json


def apply_window_truncation(items, durations_path, window_seconds):
    """Mutate `items` (list of {name,target,...}) in place; return it. No-op if
    window_seconds is falsy."""
    if not window_seconds:
        return items
    dur = json.load(open(durations_path))
    for it in items:
        d = dur.get(it["name"], 0.0)
        if d and d > window_seconds:
            n = max(1, int(len(it["target"]) * (window_seconds / d)))
            it["target"] = it["target"][:n].rstrip()
    return items
