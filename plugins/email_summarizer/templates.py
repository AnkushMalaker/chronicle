"""
Email templates for the Email Summarizer plugin.

Provides HTML and plain text email templates.
"""
import html
from datetime import datetime
from typing import Optional


def format_duration(seconds: float) -> str:
    """
    Format duration in seconds to human-readable format.

    Args:
        seconds: Duration in seconds

    Returns:
        Formatted duration (e.g., "5m 30s", "1h 15m")
    """
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)

    if hours > 0:
        return f"{hours}h {minutes}m"
    elif minutes > 0:
        return f"{minutes}m {secs}s"
    else:
        return f"{secs}s"


def format_html_email(
    summary: str,
    transcript: str,
    conversation_id: str,
    duration: float,
    created_at: Optional[datetime] = None
) -> str:
    """
    Format HTML email template.

    Args:
        summary: LLM-generated summary
        transcript: Full conversation transcript
        conversation_id: Conversation identifier
        duration: Conversation duration in seconds
        created_at: Conversation creation timestamp

    Returns:
        HTML email body
    """
    formatted_duration = format_duration(duration)
    date_str = created_at.strftime("%B %d, %Y at %I:%M %p") if created_at else "N/A"

    # Escape HTML to prevent XSS attacks
    summary_escaped = html.escape(summary, quote=True)
    transcript_escaped = html.escape(transcript, quote=True)

    # Format transcript with line breaks (after escaping)
    transcript_html = transcript_escaped.replace('\n', '<br>')

    return f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            line-height: 1.6;
            color: #333;
            max-width: 800px;
            margin: 0 auto;
            padding: 20px;
        }}
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            border-radius: 8px 8px 0 0;
            margin-bottom: 0;
        }}
        .header h1 {{
            margin: 0;
            font-size: 24px;
        }}
        .header .date {{
            margin-top: 5px;
            opacity: 0.9;
            font-size: 14px;
        }}
        .content {{
            background: #f9f9f9;
            padding: 30px;
            border: 1px solid #e0e0e0;
            border-top: none;
        }}
        .summary {{
            background: white;
            padding: 20px;
            border-radius: 6px;
            margin-bottom: 25px;
            border-left: 4px solid #667eea;
        }}
        .summary h2 {{
            margin-top: 0;
            color: #667eea;
            font-size: 18px;
        }}
        .summary p {{
            margin: 10px 0 0 0;
            line-height: 1.8;
        }}
        .transcript {{
            background: white;
            padding: 20px;
            border-radius: 6px;
            margin-bottom: 20px;
        }}
        .transcript h2 {{
            margin-top: 0;
            color: #555;
            font-size: 18px;
            border-bottom: 2px solid #e0e0e0;
            padding-bottom: 10px;
        }}
        .transcript-content {{
            font-family: 'Courier New', monospace;
            font-size: 13px;
            line-height: 1.8;
            color: #444;
            white-space: pre-wrap;
            word-wrap: break-word;
        }}
        .metadata {{
            background: white;
            padding: 15px 20px;
            border-radius: 6px;
            display: flex;
            justify-content: space-between;
            font-size: 13px;
            color: #666;
        }}
        .metadata-item {{
            display: flex;
            flex-direction: column;
        }}
        .metadata-label {{
            font-weight: bold;
            margin-bottom: 3px;
        }}
        .footer {{
            text-align: center;
            margin-top: 30px;
            padding-top: 20px;
            border-top: 1px solid #e0e0e0;
            color: #888;
            font-size: 12px;
        }}
        .footer a {{
            color: #667eea;
            text-decoration: none;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>🎙️ Conversation Summary</h1>
        <div class="date">{date_str}</div>
    </div>

    <div class="content">
        <div class="summary">
            <h2>📋 Summary</h2>
            <p>{summary_escaped}</p>
        </div>

        <div class="transcript">
            <h2>📝 Full Transcript</h2>
            <div class="transcript-content">{transcript_html}</div>
        </div>

        <div class="metadata">
            <div class="metadata-item">
                <span class="metadata-label">Duration</span>
                <span>{formatted_duration}</span>
            </div>
            <div class="metadata-item">
                <span class="metadata-label">Conversation ID</span>
                <span>{conversation_id[:12]}...</span>
            </div>
        </div>
    </div>

    <div class="footer">
        <p>
            Sent by <a href="https://github.com/chronicle-ai/chronicle">Chronicle AI</a><br>
            Your personal AI memory system
        </p>
    </div>
</body>
</html>
"""


def format_text_email(
    summary: str,
    transcript: str,
    conversation_id: str,
    duration: float,
    created_at: Optional[datetime] = None
) -> str:
    """
    Format plain text email template.

    Args:
        summary: LLM-generated summary
        transcript: Full conversation transcript
        conversation_id: Conversation identifier
        duration: Conversation duration in seconds
        created_at: Conversation creation timestamp

    Returns:
        Plain text email body
    """
    formatted_duration = format_duration(duration)
    date_str = created_at.strftime("%B %d, %Y at %I:%M %p") if created_at else "N/A"

    return f"""
🎙️ CONVERSATION SUMMARY
{date_str}

═══════════════════════════════════════════════════════════

📋 SUMMARY

{summary}

───────────────────────────────────────────────────────────

📝 FULL TRANSCRIPT

{transcript}

═══════════════════════════════════════════════════════════

📊 METADATA

Duration: {formatted_duration}
Conversation ID: {conversation_id}

───────────────────────────────────────────────────────────

Sent by Chronicle AI
Your personal AI memory system
https://github.com/chronicle-ai/chronicle
"""
