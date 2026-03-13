"""
Hourly Recap Plugin for Chronicle.

On OMI device double-click, queries the last hour's conversations,
generates a consolidated LLM recap, and emails it to the user.
"""

import html
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from advanced_omi_backend.database import get_database
from advanced_omi_backend.llm_client import async_generate
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.plugins.base import BasePlugin, PluginContext, PluginResult
from advanced_omi_backend.plugins.events import PluginEvent
from advanced_omi_backend.utils.logging_utils import mask_dict
from email_summarizer.email_service import SMTPEmailService

logger = logging.getLogger(__name__)


class HourlyRecapPlugin(BasePlugin):
    """
    Sends an email recap of recent conversations when double-click is detected.

    Subscribes to button.double_press events and:
    1. Queries conversations from the last N minutes
    2. Generates a consolidated LLM recap
    3. Emails the recap to the user

    Configuration (config/plugins.yml):
        enabled: true
        events:
          - button.double_press
        condition:
          type: always
    """

    SUPPORTED_ACCESS_LEVELS: List[str] = ["button"]

    name = "Hourly Recap"
    description = "Emails a recap of recent conversations on device double-click"

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)

        self.lookback_minutes = config.get("lookback_minutes", 60)
        self.subject_prefix = config.get("subject_prefix", "Hourly Recap")

        # Email service will be initialized in initialize()
        self.email_service: Optional[SMTPEmailService] = None

        # MongoDB database handle
        self.db = None

    def register_prompts(self, registry) -> None:
        """Register hourly recap prompts with the prompt registry."""
        registry.register_default(
            "plugin.hourly_recap.summary",
            template=(
                "You are given summaries and transcripts of multiple conversations "
                "from the last hour. Write a consolidated recap that highlights the "
                "key topics, decisions, action items, and important details across "
                "all conversations. Be concise but thorough. Use bullet points for "
                "clarity.\n\n"
                "If there is only one conversation, summarize it clearly.\n\n"
                "Conversations:\n{{conversations_block}}"
            ),
            name="Hourly Recap",
            description="Generates a consolidated recap of recent conversations.",
            category="plugin",
            plugin_id="hourly_recap",
            variables=["conversations_block"],
            is_dynamic=True,
        )

    async def initialize(self):
        if not self.enabled:
            logger.info("Hourly Recap plugin is disabled, skipping initialization")
            return

        logger.info("Initializing Hourly Recap plugin...")

        # Initialize SMTP email service
        try:
            smtp_config = {
                "smtp_host": self.config.get("smtp_host"),
                "smtp_port": self.config.get("smtp_port", 587),
                "smtp_username": self.config.get("smtp_username"),
                "smtp_password": self.config.get("smtp_password"),
                "smtp_use_tls": self.config.get("smtp_use_tls", True),
                "from_email": self.config.get("from_email"),
                "from_name": self.config.get("from_name", "Chronicle AI"),
            }

            self.email_service = SMTPEmailService(smtp_config)

            logger.info("Testing SMTP connectivity...")
            if await self.email_service.test_connection():
                logger.info("SMTP connection test successful")
            else:
                raise Exception("SMTP connection test failed")

        except Exception as e:
            logger.error(f"Failed to initialize email service: {e}")
            raise

        # Get MongoDB database handle
        self.db = get_database()
        logger.info(
            f"Hourly Recap plugin initialized (lookback={self.lookback_minutes}m)"
        )

    async def health_check(self) -> dict:
        """Test SMTP connectivity using the initialized email service."""
        import time

        if not self.email_service:
            return {"ok": False, "message": "Email service not initialized"}

        try:
            start = time.time()
            success = await self.email_service.test_connection()
            latency_ms = int((time.time() - start) * 1000)
            if success:
                return {
                    "ok": True,
                    "message": "SMTP connected",
                    "latency_ms": latency_ms,
                }
            return {
                "ok": False,
                "message": "SMTP connection failed",
                "latency_ms": latency_ms,
            }
        except Exception as e:
            return {"ok": False, "message": str(e)}

    async def cleanup(self):
        """Clean up plugin resources."""
        logger.info("Hourly Recap plugin cleanup complete")

    async def on_button_event(self, context: PluginContext) -> Optional[PluginResult]:
        """Handle double-click: query recent conversations, generate recap, send email."""
        if context.event != PluginEvent.BUTTON_DOUBLE_PRESS:
            return None

        try:
            logger.info(
                f"Hourly Recap triggered for user {context.user_id} "
                f"(lookback={self.lookback_minutes}m)"
            )

            # 1. Query recent conversations
            cutoff = datetime.utcnow() - timedelta(minutes=self.lookback_minutes)
            conversations = (
                await Conversation.find(
                    Conversation.user_id == context.user_id,
                    Conversation.created_at >= cutoff,
                    Conversation.deleted == False,  # noqa: E712
                )
                .sort(-Conversation.created_at)
                .to_list()
            )

            if not conversations:
                logger.info(
                    f"No conversations in the last {self.lookback_minutes}m "
                    f"for user {context.user_id}, skipping recap"
                )
                return PluginResult(
                    success=True,
                    message="No recent conversations to recap",
                )

            logger.info(
                f"Found {len(conversations)} conversations in the last "
                f"{self.lookback_minutes}m"
            )

            # 2. Build conversation block for LLM prompt
            conversations_block = self._build_conversations_block(conversations)

            # 3. Generate consolidated recap via LLM
            recap = await self._generate_recap(conversations_block)

            # 4. Look up user email
            user_email = await self._get_user_email(context.user_id)
            if not user_email:
                logger.warning(
                    f"No notification_email for user {context.user_id}, "
                    f"cannot send recap"
                )
                return PluginResult(
                    success=False,
                    message=f"No email configured for user {context.user_id}",
                )

            # 5. Format and send email
            subject = self._format_subject()
            body_html = self._format_html(recap, conversations)
            body_text = self._format_text(recap, conversations)

            success = await self.email_service.send_email(
                to_email=user_email,
                subject=subject,
                body_text=body_text,
                body_html=body_html,
            )

            if success:
                logger.info(
                    f"Hourly recap sent to {user_email} "
                    f"({len(conversations)} conversations)"
                )
                return PluginResult(
                    success=True,
                    message=f"Recap sent to {user_email}",
                    data={
                        "recipient": user_email,
                        "conversation_count": len(conversations),
                    },
                )
            else:
                logger.error(f"Failed to send recap to {user_email}")
                return PluginResult(
                    success=False,
                    message=f"Failed to send email to {user_email}",
                )

        except Exception as e:
            logger.error(f"Error in hourly recap plugin: {e}", exc_info=True)
            return PluginResult(success=False, message=f"Error: {str(e)}")

    def _build_conversations_block(self, conversations: List[Conversation]) -> str:
        """Build a text block summarizing each conversation for the LLM prompt."""
        parts = []
        for i, conv in enumerate(conversations, 1):
            created = conv.created_at.strftime("%I:%M %p") if conv.created_at else "N/A"
            duration = (
                f"{int(conv.audio_total_duration // 60)}m {int(conv.audio_total_duration % 60)}s"
                if conv.audio_total_duration
                else "N/A"
            )
            title = conv.title or "Untitled"
            summary = conv.summary or "No summary available"

            transcript = conv.transcript or ""
            # Truncate long transcripts to keep the prompt reasonable
            if len(transcript) > 2000:
                transcript = transcript[:2000] + "... [truncated]"

            parts.append(
                f"--- Conversation {i} ---\n"
                f"Time: {created}\n"
                f"Duration: {duration}\n"
                f"Title: {title}\n"
                f"Summary: {summary}\n"
                f"Transcript:\n{transcript}\n"
            )

        return "\n".join(parts)

    async def _generate_recap(self, conversations_block: str) -> str:
        """Generate consolidated recap via LLM."""
        try:
            from advanced_omi_backend.prompt_registry import get_prompt_registry

            registry = get_prompt_registry()
            instruction = await registry.get_prompt(
                "plugin.hourly_recap.summary",
                conversations_block=conversations_block,
            )

            logger.debug("Generating hourly recap via LLM...")
            recap = await async_generate(instruction)

            if not recap or recap.strip() == "":
                raise ValueError("LLM returned empty recap")

            logger.info("LLM recap generated successfully")
            return recap.strip()

        except Exception as e:
            logger.error(f"Failed to generate LLM recap: {e}", exc_info=True)
            # Fallback: return the raw conversations block
            logger.warning("Using fallback: raw conversation summaries")
            return conversations_block

    async def _get_user_email(self, user_id: str) -> Optional[str]:
        """Get notification email for a user."""
        try:
            from bson import ObjectId

            user = await self.db["users"].find_one({"_id": ObjectId(user_id)})
            if not user:
                logger.warning(f"User {user_id} not found")
                return None

            notification_email = user.get("notification_email")
            if not notification_email:
                logger.warning(f"User {user_id} has no notification_email set")
                return None

            return notification_email

        except Exception as e:
            logger.error(f"Error fetching user email: {e}", exc_info=True)
            return None

    def _format_subject(self) -> str:
        """Format email subject line."""
        now = datetime.utcnow().strftime("%b %d, %Y at %I:%M %p")
        return f"{self.subject_prefix} - {now}"

    def _format_html(self, recap: str, conversations: List[Conversation]) -> str:
        """Format HTML email body."""
        now_str = datetime.utcnow().strftime("%B %d, %Y at %I:%M %p")
        recap_escaped = html.escape(recap, quote=True).replace("\n", "<br>")

        # Build conversation list items
        conv_items = ""
        for conv in conversations:
            title = html.escape(conv.title or "Untitled", quote=True)
            created = conv.created_at.strftime("%I:%M %p") if conv.created_at else "N/A"
            duration = (
                f"{int(conv.audio_total_duration // 60)}m {int(conv.audio_total_duration % 60)}s"
                if conv.audio_total_duration
                else "N/A"
            )
            summary = html.escape(conv.summary or "No summary", quote=True)
            conv_items += f"""
            <tr>
                <td style="padding: 8px 12px; border-bottom: 1px solid #eee;">{created}</td>
                <td style="padding: 8px 12px; border-bottom: 1px solid #eee;"><strong>{title}</strong><br><span style="color:#666;font-size:13px;">{summary}</span></td>
                <td style="padding: 8px 12px; border-bottom: 1px solid #eee;">{duration}</td>
            </tr>"""

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
            background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
            color: white;
            padding: 30px;
            border-radius: 8px 8px 0 0;
        }}
        .header h1 {{ margin: 0; font-size: 24px; }}
        .header .date {{ margin-top: 5px; opacity: 0.9; font-size: 14px; }}
        .content {{
            background: #f9f9f9;
            padding: 30px;
            border: 1px solid #e0e0e0;
            border-top: none;
        }}
        .recap {{
            background: white;
            padding: 20px;
            border-radius: 6px;
            margin-bottom: 25px;
            border-left: 4px solid #f5576c;
        }}
        .recap h2 {{ margin-top: 0; color: #f5576c; font-size: 18px; }}
        .conversations {{ background: white; padding: 20px; border-radius: 6px; }}
        .conversations h2 {{ margin-top: 0; color: #555; font-size: 18px; border-bottom: 2px solid #e0e0e0; padding-bottom: 10px; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th {{ text-align: left; padding: 8px 12px; border-bottom: 2px solid #ddd; color: #666; font-size: 13px; }}
        .footer {{
            text-align: center;
            margin-top: 30px;
            padding-top: 20px;
            border-top: 1px solid #e0e0e0;
            color: #888;
            font-size: 12px;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>Hourly Recap</h1>
        <div class="date">{now_str} &middot; {len(conversations)} conversation(s)</div>
    </div>

    <div class="content">
        <div class="recap">
            <h2>Recap</h2>
            <p>{recap_escaped}</p>
        </div>

        <div class="conversations">
            <h2>Conversations</h2>
            <table>
                <tr>
                    <th>Time</th>
                    <th>Details</th>
                    <th>Duration</th>
                </tr>
                {conv_items}
            </table>
        </div>
    </div>

    <div class="footer">
        <p>
            Sent by <a href="https://github.com/chronicle-ai/chronicle" style="color:#f5576c;text-decoration:none;">Chronicle AI</a><br>
            Your personal AI memory system
        </p>
    </div>
</body>
</html>
"""

    def _format_text(self, recap: str, conversations: List[Conversation]) -> str:
        """Format plain text email body."""
        now_str = datetime.utcnow().strftime("%B %d, %Y at %I:%M %p")

        conv_lines = []
        for i, conv in enumerate(conversations, 1):
            title = conv.title or "Untitled"
            created = conv.created_at.strftime("%I:%M %p") if conv.created_at else "N/A"
            duration = (
                f"{int(conv.audio_total_duration // 60)}m {int(conv.audio_total_duration % 60)}s"
                if conv.audio_total_duration
                else "N/A"
            )
            summary = conv.summary or "No summary"
            conv_lines.append(
                f"  {i}. [{created}] {title} ({duration})\n     {summary}"
            )

        conv_block = "\n".join(conv_lines)

        return f"""
HOURLY RECAP
{now_str} - {len(conversations)} conversation(s)

================================================================

RECAP

{recap}

----------------------------------------------------------------

CONVERSATIONS

{conv_block}

================================================================

Sent by Chronicle AI
Your personal AI memory system
https://github.com/chronicle-ai/chronicle
"""

    @staticmethod
    async def test_connection(config: Dict[str, Any]) -> Dict[str, Any]:
        """Test SMTP connection with provided configuration."""
        import time

        try:
            required_fields = [
                "smtp_host",
                "smtp_username",
                "smtp_password",
                "from_email",
            ]
            missing_fields = [f for f in required_fields if not config.get(f)]

            if missing_fields:
                return {
                    "success": False,
                    "message": f"Missing required fields: {', '.join(missing_fields)}",
                    "status": "error",
                }

            smtp_config = {
                "smtp_host": config.get("smtp_host"),
                "smtp_port": config.get("smtp_port", 587),
                "smtp_username": config.get("smtp_username"),
                "smtp_password": config.get("smtp_password"),
                "smtp_use_tls": config.get("smtp_use_tls", True),
                "from_email": config.get("from_email"),
                "from_name": config.get("from_name", "Chronicle AI"),
            }

            logger.debug(f"SMTP config for testing: {mask_dict(smtp_config)}")

            email_service = SMTPEmailService(smtp_config)

            logger.info(f"Testing SMTP connection to {smtp_config['smtp_host']}...")
            start_time = time.time()

            connection_success = await email_service.test_connection()
            connection_time_ms = int((time.time() - start_time) * 1000)

            if connection_success:
                return {
                    "success": True,
                    "message": f"Successfully connected to SMTP server at {smtp_config['smtp_host']}",
                    "status": "success",
                    "details": {
                        "smtp_host": smtp_config["smtp_host"],
                        "smtp_port": smtp_config["smtp_port"],
                        "connection_time_ms": connection_time_ms,
                        "use_tls": smtp_config["smtp_use_tls"],
                    },
                }
            else:
                return {
                    "success": False,
                    "message": "SMTP connection test failed",
                    "status": "error",
                }

        except Exception as e:
            logger.error(f"SMTP connection test failed: {e}", exc_info=True)
            return {
                "success": False,
                "message": f"Connection test failed: {str(e)}",
                "status": "error",
            }
