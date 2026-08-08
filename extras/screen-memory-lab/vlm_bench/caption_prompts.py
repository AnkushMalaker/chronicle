"""The caption prompts, in one place so local and cloud runs provably share them.

Both prompts are pure description. Neither carries an authorship or attribution
rule, on purpose: docs/research/screen-memory/07-how-real-systems-do-it.md claims
a description is true regardless of whose screen it is, and that claim is only
testable if the rule is absent. See caption_frames.py for the rest of the setup.
"""

from __future__ import annotations

PROMPTS = {
    # Plain description. The retrieval baseline.
    "caption": (
        "Write a description of this screenshot so that it can be found later by "
        "someone searching their own screen history.\n\n"
        "Describe what is visible: the application or kind of screen, the main "
        "content, and any large or prominent text. Use the specific words a person "
        "would search for, including proper nouns you can read. Describe only what "
        "you can actually see on the screen.\n\n"
        "Reply with 2 to 4 sentences of plain prose. No preamble, no bullet points."
    ),
    # Same, plus prominent text verbatim. Tests whether reading the big text out
    # explicitly helps retrieval over prose alone -- result banners are exactly
    # the case where stored OCR is sometimes empty.
    "caption_text": (
        "Describe this screenshot in 2 to 3 sentences so that it can be found later "
        "by someone searching their own screen history. Describe only what you can "
        "actually see.\n\n"
        "Then on a new line starting with 'TEXT:' list the largest and most "
        "prominent pieces of text visible on the screen, copied exactly as written, "
        "separated by ' | '. If there is no prominent text, write 'TEXT: none'."
    ),
}
