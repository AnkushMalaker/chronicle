"""
Email Summarizer Plugin for Chronicle.

Automatically sends email summaries after memory extraction.
"""
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from advanced_omi_backend.database import get_database
from advanced_omi_backend.utils.logging_utils import mask_dict

from advanced_omi_backend.plugins.base import BasePlugin, PluginContext, PluginResult
from .email_service import SMTPEmailService
from .templates import format_html_email, format_text_email

logger = logging.getLogger(__name__)


class EmailSummarizerPlugin(BasePlugin):
    """
    Plugin for sending email summaries after memory extraction.

    Subscribes to memory.processed events and:
    1. Fetches conversation from DB (title, summary, transcript are ready by this point)
    2. Retrieves user email from event data or database
    3. Formats HTML and plain text emails
    4. Sends email via SMTP
    """

    SUPPORTED_ACCESS_LEVELS: List[str] = ['conversation']

    name = "Email Summarizer"
    description = "Sends email summaries after memory extraction"

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize Email Summarizer plugin.

        Args:
            config: Plugin configuration from config/plugins.yml
        """
        super().__init__(config)

        self.subject_prefix = config.get('subject_prefix', 'Conversation Summary')
        self.include_conversation_id = config.get('include_conversation_id', True)
        self.include_duration = config.get('include_duration', True)

        # Email service will be initialized in initialize()
        self.email_service: Optional[SMTPEmailService] = None

        # MongoDB database handle
        self.db = None

    def register_prompts(self, registry) -> None:
        """Register email summarizer prompts with the prompt registry."""
        registry.register_default(
            "plugin.email_summarizer.summary",
            template=(
                "Summarize this conversation in {{summary_max_sentences}} sentences or less. "
                "Focus on key points, main topics discussed, and any action items or decisions. "
                "Be concise and clear."
            ),
            name="Email Summary",
            description="Generates a concise email summary of a completed conversation.",
            category="plugin",
            plugin_id="email_summarizer",
            variables=["summary_max_sentences"],
            is_dynamic=True,
        )

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

    async def on_memory_processed(self, context: PluginContext) -> Optional[PluginResult]:
        """
        Send email summary after memory extraction completes.

        By this point the conversation has its title, summary, and transcript
        already generated in the DB, so no extra LLM call is needed.

        Args:
            context: Plugin context with memory event data
                - conversation_id: str
                - conversation: dict with conversation_id, user_id, user_email
                - memories: list of memory IDs
                - memory_count: int
        """
        try:
            conversation_id = context.data.get('conversation_id', 'unknown')
            memory_count = context.data.get('memory_count', 0)
            logger.info(
                f"Processing memory.processed event for user: {context.user_id}, "
                f"conversation: {conversation_id}, memories: {memory_count}"
            )

            # Fetch full conversation from DB (has title, summary, transcript by now)
            conv_doc = await self.db['conversations'].find_one(
                {"conversation_id": conversation_id}
            )
            if not conv_doc:
                logger.warning(f"Conversation {conversation_id} not found in DB, skipping email")
                return PluginResult(success=False, message="Conversation not found")

            # Get transcript from active version
            transcript = ""
            active_version = conv_doc.get("active_transcript_version", 0)
            for tv in conv_doc.get("transcript_versions", []):
                if tv.get("version") == active_version:
                    transcript = tv.get("transcript", "")
                    break

            if not transcript:
                logger.warning(f"No transcript for conversation {conversation_id}, skipping email")
                return PluginResult(success=False, message="Skipped: Empty transcript")

            # Use the DB summary (already generated by this point)
            summary = conv_doc.get("detailed_summary") or conv_doc.get("summary")
            if not summary:
                logger.warning(f"No summary for conversation {conversation_id}, skipping email")
                return PluginResult(success=False, message="Skipped: No summary available")

            title = conv_doc.get("title") or self.subject_prefix

            # Get user email — prefer event data, fall back to DB lookup
            user_email = context.data.get('conversation', {}).get('user_email')
            if not user_email:
                user_email = await self._get_user_email(context.user_id)
            if not user_email:
                return PluginResult(
                    success=False,
                    message=f"No email configured for user {context.user_id}",
                )

            # Format and send
            created_at = conv_doc.get("created_at")
            duration = conv_doc.get("duration", 0)

            subject = self._format_subject(created_at)
            body_html = format_html_email(
                summary=summary,
                transcript=transcript,
                conversation_id=conversation_id,
                duration=duration,
                created_at=created_at,
            )
            body_text = format_text_email(
                summary=summary,
                transcript=transcript,
                conversation_id=conversation_id,
                duration=duration,
                created_at=created_at,
            )

            success = await self.email_service.send_email(
                to_email=user_email,
                subject=subject,
                body_text=body_text,
                body_html=body_html,
            )

            if success:
                logger.info(f"✅ Email summary sent to {user_email} for conversation {conversation_id}")
                return PluginResult(
                    success=True,
                    message=f"Email sent to {user_email}",
                    data={
                        'recipient': user_email,
                        'conversation_id': conversation_id,
                        'title': title,
                        'memory_count': memory_count,
                    },
                )
            else:
                logger.error(f"Failed to send email to {user_email}")
                return PluginResult(success=False, message=f"Failed to send email to {user_email}")

        except Exception as e:
            logger.error(f"Error in email summarizer (memory.processed): {e}", exc_info=True)
            return PluginResult(success=False, message=f"Error: {str(e)}")

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

    @staticmethod
    async def test_connection(config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Test SMTP connection with provided configuration.

        This static method tests the SMTP connection without fully initializing the plugin.
        Used by the form-based configuration UI to validate settings before saving.

        Args:
            config: Configuration dictionary with SMTP settings

        Returns:
            Dict with success status, message, and optional details

        Example:
            >>> result = await EmailSummarizerPlugin.test_connection({
            ...     'smtp_host': 'smtp.gmail.com',
            ...     'smtp_port': 587,
            ...     'smtp_username': 'user@gmail.com',
            ...     'smtp_password': 'password',
            ...     'smtp_use_tls': True,
            ...     'from_email': 'noreply@example.com',
            ...     'from_name': 'Test'
            ... })
            >>> result['success']
            True
        """
        import time

        try:
            # Validate required config fields
            required_fields = ['smtp_host', 'smtp_username', 'smtp_password', 'from_email']
            missing_fields = [field for field in required_fields if not config.get(field)]

            if missing_fields:
                return {
                    "success": False,
                    "message": f"Missing required fields: {', '.join(missing_fields)}",
                    "status": "error"
                }

            # Build SMTP config
            smtp_config = {
                'smtp_host': config.get('smtp_host'),
                'smtp_port': config.get('smtp_port', 587),
                'smtp_username': config.get('smtp_username'),
                'smtp_password': config.get('smtp_password'),
                'smtp_use_tls': config.get('smtp_use_tls', True),
                'from_email': config.get('from_email'),
                'from_name': config.get('from_name', 'Chronicle AI'),
            }

            # Log config with masked secrets for debugging
            logger.debug(f"SMTP config for testing: {mask_dict(smtp_config)}")

            # Create temporary email service instance
            email_service = SMTPEmailService(smtp_config)

            # Test connection
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
                        "smtp_host": smtp_config['smtp_host'],
                        "smtp_port": smtp_config['smtp_port'],
                        "connection_time_ms": connection_time_ms,
                        "use_tls": smtp_config['smtp_use_tls']
                    }
                }
            else:
                return {
                    "success": False,
                    "message": "SMTP connection test failed",
                    "status": "error"
                }

        except Exception as e:
            logger.error(f"SMTP connection test failed: {e}", exc_info=True)
            error_msg = str(e)
            
            # Provide helpful hints based on error type
            hints = []
            if "Authentication" in error_msg or "535" in error_msg:
                hints.append("For Gmail: Enable 2FA and create an App Password at https://myaccount.google.com/apppasswords")
                hints.append("Verify your username and password are correct")
            elif "Connection" in error_msg or "timeout" in error_msg.lower():
                hints.append("Check your SMTP host and port settings")
                hints.append("Verify firewall/network allows outbound SMTP connections")
            elif "TLS" in error_msg or "SSL" in error_msg:
                hints.append("For port 587: Enable TLS")
                hints.append("For port 465: Disable TLS (uses implicit SSL)")
            
            return {
                "success": False,
                "message": f"Connection test failed: {error_msg}",
                "status": "error",
                "hints": hints
            }
