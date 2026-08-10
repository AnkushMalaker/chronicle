"""User-facing email timestamps use the recipient's configured timezone."""

import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

PLUGIN_ROOT = Path(__file__).resolve().parents[3] / "plugins"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from email_summarizer.templates import format_html_email, format_text_email
from hourly_recap.plugin import HourlyRecapPlugin

from advanced_omi_backend.utils.user_time import format_user_datetime


def test_naive_mongo_timestamp_is_rendered_in_user_timezone():
    stored_utc = datetime(2026, 8, 10, 3, 30)

    rendered = format_user_datetime(
        stored_utc, "Asia/Kolkata", "%B %d, %Y at %I:%M %p %Z"
    )

    assert rendered == "August 10, 2026 at 09:00 AM IST"


def test_summary_email_templates_render_recipient_local_time():
    stored_utc = datetime(2026, 8, 10, 3, 30, tzinfo=UTC)
    kwargs = {
        "summary": "Summary",
        "transcript": "Transcript",
        "conversation_id": "conversation-123",
        "duration": 60,
        "created_at": stored_utc,
        "timezone_name": "America/New_York",
    }

    html = format_html_email(**kwargs)
    text = format_text_email(**kwargs)

    assert "August 09, 2026 at 11:30 PM EDT" in html
    assert "August 09, 2026 at 11:30 PM EDT" in text
    assert "August 10, 2026 at 03:30 AM" not in html
    assert "August 10, 2026 at 03:30 AM" not in text


def test_hourly_recap_conversation_times_use_recipient_timezone():
    plugin = HourlyRecapPlugin({})
    conversation = SimpleNamespace(
        created_at=datetime(2026, 8, 10, 3, 30),
        audio_total_duration=60,
        title="Late conversation",
        summary="Summary",
        transcript="Transcript",
    )

    prompt_block = plugin._build_conversations_block([conversation], "America/New_York")
    html = plugin._format_html("Recap", [conversation], "America/New_York")
    text = plugin._format_text("Recap", [conversation], "America/New_York")

    assert "11:30 PM EDT" in prompt_block
    assert "11:30 PM EDT" in html
    assert "11:30 PM EDT" in text
