from datetime import datetime, timedelta, timezone

from advanced_omi_backend.routers.modules.device_input_routes import ActivityItem
from advanced_omi_backend.services import device_context
from advanced_omi_backend.services.device_context import (
    _expired_conversation_ids,
    select_context_items,
)

BASE = datetime(2026, 7, 26, 10, 0, tzinfo=timezone.utc)


def frame(
    index: int,
    text: str,
    app: str = "Zen",
    window: str = "Chronicle — Timeline",
    text_source: str = "accessibility",
    capture_trigger: str = "visual_change",
):
    return ActivityItem(
        source_item_id=f"frame:{index}",
        captured_at=BASE + timedelta(seconds=index),
        metadata={
            "frame_id": index,
            "app_name": app,
            "window_name": window,
            "text": text,
            "text_source": text_source,
            "capture_trigger": capture_trigger,
        },
    )


def select(items, max_bytes=4_000_000, similarity_threshold=0.85):
    return select_context_items(
        items, max_bytes=max_bytes, similarity_threshold=similarity_threshold
    )


def test_identical_consecutive_frames_collapse_to_one():
    kept, dropped = select([frame(i, "conversation timeline loaded") for i in range(5)])

    assert [item.source_item_id for item in kept] == ["frame:0"]
    assert dropped["duplicate"] == 4


# A real OCR frame is a whole screen of text, so the fixtures below are sized
# like one. A short string moves token overlap far too much to be representative.
SCREEN_WORDS = [f"word{index}" for index in range(60)]


def screen_text(replaced: int = 0) -> str:
    """A screenful of OCR with `replaced` words swapped out for new ones."""
    return " ".join(
        SCREEN_WORDS[: len(SCREEN_WORDS) - replaced]
        + [f"fresh{index}" for index in range(replaced)]
    )


def test_near_identical_frames_of_the_same_window_collapse():
    kept, dropped = select([frame(0, screen_text()), frame(1, screen_text(replaced=1))])

    assert len(kept) == 1
    assert dropped["duplicate"] == 1


def test_a_genuinely_different_screen_is_kept():
    kept, dropped = select(
        [
            frame(0, "inbox unread 3 messages from ankush about the collector"),
            frame(
                1, "vad gate rejected the silent output session before transcription"
            ),
        ]
    )

    assert [item.source_item_id for item in kept] == ["frame:0", "frame:1"]
    assert dropped["duplicate"] == 0


def test_same_text_in_a_different_window_is_kept():
    kept, _ = select(
        [
            frame(0, "device_context.py", window="Zed — device_context.py"),
            frame(
                1, "device_context.py", app="Zen", window="GitHub — device_context.py"
            ),
        ]
    )

    assert len(kept) == 2


def test_drift_accumulates_against_the_last_kept_frame():
    """No step differs enough on its own, but the screen still ends up elsewhere.

    Comparing each frame against its immediate predecessor would drop all three
    and lose the change entirely; comparing against the last *kept* frame lets
    the drift add up until it is worth storing.
    """
    kept, dropped = select(
        [frame(step, screen_text(replaced=4 * step)) for step in range(3)]
    )

    assert [item.source_item_id for item in kept] == ["frame:0", "frame:2"]
    assert dropped["duplicate"] == 1


def test_frames_with_neither_text_nor_identity_are_dropped():
    blank = ActivityItem(
        source_item_id="frame:9",
        captured_at=BASE,
        metadata={"frame_id": 9, "app_name": "", "window_name": "", "text": ""},
    )
    kept, dropped = select([blank])

    assert kept == []
    assert dropped["empty"] == 1


def test_contextless_ocr_with_text_survives():
    """The KWin NULL-app case: OCR is the only record a fullscreen session happened."""
    game = ActivityItem(
        source_item_id="frame:11",
        captured_at=BASE,
        metadata={
            "frame_id": 11,
            "app_name": None,
            "window_name": None,
            "text": "Victory",
        },
    )
    kept, dropped = select([game])

    assert [item.source_item_id for item in kept] == ["frame:11"]
    assert dropped["empty"] == 0


def game_frame(index: int, text: str):
    """KDE Wayland exposes no accessibility tree for a fullscreen window."""
    return ActivityItem(
        source_item_id=f"frame:{index}",
        captured_at=BASE + timedelta(seconds=index),
        metadata={
            "frame_id": index,
            "app_name": "",
            "window_name": "",
            "text": text,
            "text_source": "ocr",
            "capture_trigger": "visual_change",
        },
    )


def test_ocr_frames_are_exempt_even_when_the_window_is_known():
    """ScreenPipe got the app name but fell back to OCR — text is still noisy.

    Measured on real data: this rescues 114 frames, 104 of them in the editor.
    """
    kept, _ = select(
        [
            frame(0, screen_text(), app="dev.zed.Zed", text_source="ocr"),
            frame(1, screen_text(replaced=1), app="dev.zed.Zed", text_source="ocr"),
        ]
    )

    assert len(kept) == 2


def test_a_manual_capture_is_never_collapsed():
    """ScreenPipe recorded an explicit reason for grabbing this frame."""
    kept, _ = select(
        [
            frame(0, screen_text()),
            frame(1, screen_text(replaced=1), capture_trigger="manual"),
        ]
    )

    assert [item.source_item_id for item in kept] == ["frame:0", "frame:1"]


def test_a_focus_change_is_never_collapsed():
    kept, _ = select(
        [
            frame(0, screen_text()),
            frame(1, screen_text(replaced=1), capture_trigger="window_focus"),
        ]
    )

    assert len(kept) == 2


def test_short_phrases_require_an_exact_match():
    """ScreenPipe's own rule: under four words, only identical text is a duplicate."""
    kept, _ = select([frame(0, "build passed"), frame(1, "build failed")])

    assert len(kept) == 2


def test_known_limit_a_small_addition_to_a_large_screen_is_lost():
    """Documents a real gap rather than asserting it away.

    Word-set overlap barely moves when a long screen gains a little: a 60-word
    page plus 9 new words scores 0.87, over the 0.85 threshold, so the new
    message is dropped. Inherent to the metric — ScreenPipe chose it for audio
    dedup, where either copy of a duplicate is equally good. Their answer to
    accumulated drift is the 60s TTL in `TreeCache`, which forces a store
    regardless of similarity; nothing equivalent runs here yet.
    """
    kept, _ = select(
        [
            frame(0, screen_text(), window="Slack — #chronicle"),
            frame(
                1,
                screen_text() + " and here is a genuinely new message from a teammate",
                window="Slack — #chronicle",
            ),
        ]
    )

    assert len(kept) == 1  # the addition is lost — see docstring


def test_similar_sparse_game_frames_are_both_kept():
    """Two moments of one game read almost the same but are not the same screen.

    Sparse HUD fragments make token overlap meaningless — it misses on identical
    screens and hits on unrelated ones — so overlap must not decide here. These
    frames are ~4% of stored bytes and are often the only record that a
    fullscreen session happened, so the tie breaks toward keeping them.
    """
    kept, report = select(
        [
            game_frame(0, "2 V Delete BARRACKS 14:02"),
            game_frame(1, "2 V Delete BARRACKS 14:09"),
        ]
    )

    assert len(kept) == 2
    assert report["duplicate"] == 0


def test_byte_identical_sparse_frames_still_collapse():
    """Exact duplicates need no judgement, so they go everywhere."""
    kept, report = select([game_frame(i, "2 V Delete BARRACKS") for i in range(4)])

    assert len(kept) == 1
    assert report["duplicate"] == 3


def test_text_dense_frames_in_a_known_window_still_collapse():
    """The filter keeps its teeth where overlap is actually evidence."""
    kept, _ = select([frame(index, screen_text()) for index in range(4)])

    assert len(kept) == 1


def unrelated_text(index: int) -> str:
    """A dense screen sharing no vocabulary with any other, so dedup can't merge it."""
    return " ".join(f"screen{index}word{position}" for position in range(50))


def test_the_byte_budget_bounds_storage():
    frames = [frame(index, unrelated_text(index)) for index in range(40)]

    kept, report = select(frames, max_bytes=2000)

    assert 0 < len(kept) < 40
    assert report["over_budget"] == 40 - len(kept)
    assert report["kept_bytes"] <= 2000


def test_items_are_ordered_by_capture_time_before_filtering():
    kept, _ = select([frame(2, "later screen"), frame(0, "earlier screen")])

    assert [item.source_item_id for item in kept] == ["frame:0", "frame:2"]


class FakeConversation:
    def __init__(self, conversation_id, deleted=False, deleted_at=None):
        self.conversation_id = conversation_id
        self.deleted = deleted
        self.deleted_at = deleted_at


class FakeFind:
    def __init__(self, rows):
        self._rows = rows

    async def to_list(self):
        return self._rows


def patch_conversations(monkeypatch, rows):
    def find(query):
        wanted = query["conversation_id"]["$in"]
        return FakeFind([row for row in rows if row.conversation_id in wanted])

    monkeypatch.setattr(device_context.Conversation, "find", staticmethod(find))


async def test_hard_deleted_conversations_expire_immediately(monkeypatch):
    patch_conversations(monkeypatch, [])

    expired = await _expired_conversation_ids(["gone-1"], BASE)

    assert expired == ["gone-1"]


async def test_recently_soft_deleted_conversations_are_kept(monkeypatch):
    """A soft delete is restorable, so its context waits out the retention window."""
    patch_conversations(
        monkeypatch,
        [FakeConversation("conv-1", deleted=True, deleted_at=BASE + timedelta(days=1))],
    )

    assert await _expired_conversation_ids(["conv-1"], BASE) == []


async def test_soft_deleted_conversations_expire_past_the_cutoff(monkeypatch):
    patch_conversations(
        monkeypatch,
        [FakeConversation("conv-1", deleted=True, deleted_at=BASE - timedelta(days=1))],
    )

    assert await _expired_conversation_ids(["conv-1"], BASE) == ["conv-1"]


async def test_live_conversations_keep_their_context(monkeypatch):
    patch_conversations(monkeypatch, [FakeConversation("conv-1")])

    assert await _expired_conversation_ids(["conv-1"], BASE) == []


async def test_naive_mongo_delete_timestamps_compare_as_utc(monkeypatch):
    patch_conversations(
        monkeypatch,
        [
            FakeConversation(
                "conv-1", deleted=True, deleted_at=datetime(2026, 7, 25, 10, 0)
            )
        ],
    )

    assert await _expired_conversation_ids(["conv-1"], BASE) == ["conv-1"]
