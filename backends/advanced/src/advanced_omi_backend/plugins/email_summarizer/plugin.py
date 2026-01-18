"""
Email Summarizer Plugin for Chronicle.

Automatically sends email summaries when conversations complete.
"""
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from advanced_omi_backend.database import get_database
from advanced_omi_backend.llm_client import async_generate

from ..base import BasePlugin, PluginContext, PluginResult
from .email_service import SMTPEmailService
from .templates import format_html_email, format_text_email

logger = logging.getLogger(__name__)


class EmailSummarizerPlugin(BasePlugin):
    """
    Plugin for sending email summaries when conversations complete.

    Subscribes to conversation.complete events and:
    1. Retrieves user email from database
    2. Generates LLM summary of the conversation
    3. Formats HTML and plain text emails
    4. Sends email via SMTP

    Configuration (config/plugins.yml):
        enabled: true
        events:
          - conversation.complete
        condition:
          type: always
        smtp_host: smtp.gmail.com
        smtp_port: 587
        smtp_username: ${SMTP_USERNAME}
        smtp_password: ${SMTP_PASSWORD}
        smtp_use_tls: true
        from_email: noreply@chronicle.ai
        from_name: Chronicle AI
        subject_prefix: "Conversation Summary"
        summary_max_sentences: 3
    """

    SUPPORTED_ACCESS_LEVELS: List[str] = ['conversation']

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize Email Summarizer plugin.

        Args:
            config: Plugin configuration from config/plugins.yml
        """
        super().__init__(config)

        self.subject_prefix = config.get('subject_prefix', 'Conversation Summary')
        self.summary_max_sentences = config.get('summary_max_sentences', 3)
        self.include_conversation_id = config.get('include_conversation_id', True)
        self.include_duration = config.get('include_duration', True)

        # Email service will be initialized in initialize()
        self.email_service: Optional[SMTPEmailService] = None

        # MongoDB database handle
        self.db = None

    async def initialize(self):
        """
        Initialize plugin resources.

        Sets up SMTP email service and MongoDB connection.

        Raises:
            ValueError: If SMTP configuration is incomplete
            Exception: If email service initialization fails
        """
        if not self.enabled:
            logger.info("Email Summarizer plugin is disabled, skipping initialization")
            return

        logger.info("Initializing Email Summarizer plugin...")

        # Initialize SMTP email service
        try:
            smtp_config = {
                'smtp_host': self.config.get('smtp_host'),
                'smtp_port': self.config.get('smtp_port', 587),
                'smtp_username': self.config.get('smtp_username'),
                'smtp_password': self.config.get('smtp_password'),
                'smtp_use_tls': self.config.get('smtp_use_tls', True),
                'from_email': self.config.get('from_email'),
                'from_name': self.config.get('from_name', 'Chronicle AI'),
            }

            self.email_service = SMTPEmailService(smtp_config)

            # Test SMTP connection
            logger.info("Testing SMTP connectivity...")
            if await self.email_service.test_connection():
                logger.info("✅ SMTP connection test successful")
            else:
                raise Exception("SMTP connection test failed")

        except Exception as e:
            logger.error(f"Failed to initialize email service: {e}")
            raise

        # Get MongoDB database handle
        self.db = get_database()
        logger.info("✅ Email Summarizer plugin initialized successfully")

    async def cleanup(self):
        """Clean up plugin resources."""
        logger.info("Email Summarizer plugin cleanup complete")

    async def on_conversation_complete(self, context: PluginContext) -> Optional[PluginResult]:
        """
        Send email summary when conversation completes.

        Args:
            context: Plugin context with conversation data
                - conversation: dict - Full conversation data
                - transcript: str - Complete transcript
                - duration: float - Conversation duration
                - conversation_id: str - Conversation identifier

        Returns:
            PluginResult with success status and message
        """
        try:
            logger.info(f"Processing conversation complete event for user: {context.user_id}")

            # Extract conversation data
            conversation = context.data.get('conversation', {})
            transcript = context.data.get('transcript', '')
            duration = context.data.get('duration', 0)
            conversation_id = context.data.get('conversation_id', 'unknown')
            created_at = conversation.get('created_at')

            # Validate transcript exists
            if not transcript or transcript.strip() == '':
                logger.warning(f"Empty transcript for conversation {conversation_id}, skipping email")
                return PluginResult(
                    success=False,
                    message="Skipped: Empty transcript"
                )

            # Get user email from database
            user_email = await self._get_user_email(context.user_id)
            if not user_email:
                logger.warning(f"No email found for user {context.user_id}, cannot send summary")
                return PluginResult(
                    success=False,
                    message=f"No email configured for user {context.user_id}"
                )

            # Generate LLM summary
            summary = await self._generate_summary(transcript)

            # Format email subject and body
            subject = self._format_subject(created_at)
            body_html = format_html_email(
                summary=summary,
                transcript=transcript,
                conversation_id=conversation_id,
                duration=duration,
                created_at=created_at
            )
            body_text = format_text_email(
                summary=summary,
                transcript=transcript,
                conversation_id=conversation_id,
                duration=duration,
                created_at=created_at
            )

            # Send email
            success = await self.email_service.send_email(
                to_email=user_email,
                subject=subject,
                body_text=body_text,
                body_html=body_html
            )

            if success:
                logger.info(f"✅ Email summary sent to {user_email} for conversation {conversation_id}")
                return PluginResult(
                    success=True,
                    message=f"Email sent to {user_email}",
                    data={'recipient': user_email, 'conversation_id': conversation_id}
                )
            else:
                logger.error(f"Failed to send email to {user_email}")
                return PluginResult(
                    success=False,
                    message=f"Failed to send email to {user_email}"
                )

        except Exception as e:
            logger.error(f"Error in email summarizer plugin: {e}", exc_info=True)
            return PluginResult(
                success=False,
                message=f"Error: {str(e)}"
            )

    async def _get_user_email(self, user_id: str) -> Optional[str]:
        """
        Get notification email from user.

        Args:
            user_id: User identifier (MongoDB ObjectId)

        Returns:
            User's notification_email, or None if not set
        """
        try:
            from bson import ObjectId

            # Query users collection
            user = await self.db['users'].find_one({'_id': ObjectId(user_id)})

            if not user:
                logger.warning(f"User {user_id} not found")
                return None

            notification_email = user.get('notification_email')

            if not notification_email:
                logger.warning(f"User {user_id} has no notification_email set")
                return None

            logger.debug(f"Sending notification to {notification_email} for user {user_id}")
            return notification_email

        except Exception as e:
            logger.error(f"Error fetching user email: {e}", exc_info=True)
            return None

    async def _generate_summary(self, transcript: str) -> str:
        """
        Generate LLM summary of the conversation.

        Args:
            transcript: Full conversation transcript

        Returns:
            Generated summary (2-3 sentences)
        """
        try:
            prompt = (
                f"Summarize this conversation in {self.summary_max_sentences} sentences or less. "
                f"Focus on key points, main topics discussed, and any action items or decisions. "
                f"Be concise and clear.\n\n"
                f"Conversation:\n{transcript}"
            )

            logger.debug("Generating LLM summary...")
            summary = await async_generate(prompt)

            if not summary or summary.strip() == '':
                raise ValueError("LLM returned empty summary")

            logger.info("✅ LLM summary generated successfully")
            return summary.strip()

        except Exception as e:
            logger.error(f"Failed to generate LLM summary: {e}", exc_info=True)
            # Fallback: return first 300 characters of transcript
            logger.warning("Using fallback: truncated transcript")
            return transcript[:300] + "..." if len(transcript) > 300 else transcript

    def _format_subject(self, created_at: Optional[datetime] = None) -> str:
        """
        Format email subject line.

        Args:
            created_at: Conversation creation timestamp

        Returns:
            Formatted subject line
        """
        if created_at:
            date_str = created_at.strftime("%b %d, %Y at %I:%M %p")
            return f"{self.subject_prefix} - {date_str}"
        else:
            return self.subject_prefix
