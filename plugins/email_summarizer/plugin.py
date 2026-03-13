"""
Email Summarizer Plugin for Chronicle.

Automatically sends email summaries after memory extraction.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.plugins.base import BasePlugin, PluginContext, PluginResult
from advanced_omi_backend.utils.logging_utils import mask_dict

from .email_service import SMTPEmailService
from .templates import format_html_email, format_text_email

logger = logging.getLogger(__name__)


class EmailSummarizerPlugin(BasePlugin):
    """
    Plugin for sending email summaries after conversation processing completes.

    Subscribes to conversation.complete events (which fire after all processing
    jobs finish: speaker recognition, memory extraction, and title/summary
    generation). This ensures the email contains the final title, summary with
    speaker names, and detailed summary.

    Flow:
    1. Fetches conversation from DB (all fields are finalized by this point)
    2. Retrieves user email from SMTP config
    3. Formats HTML and plain text emails
    4. Sends email via SMTP
    """

    SUPPORTED_ACCESS_LEVELS: List[str] = ["conversation"]

    name = "Email Summarizer"
    description = "Sends email summaries after memory extraction"

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize Email Summarizer plugin.

        Args:
            config: Plugin configuration from config/plugins.yml
        """
        super().__init__(config)

        self.subject_prefix = config.get("subject_prefix", "Conversation Summary")
        self.include_conversation_id = config.get("include_conversation_id", True)
        self.include_duration = config.get("include_duration", True)

        # Email service will be initialized in initialize()
        self.email_service: Optional[SMTPEmailService] = None

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
                "smtp_host": self.config.get("smtp_host"),
                "smtp_port": self.config.get("smtp_port", 587),
                "smtp_username": self.config.get("smtp_username"),
                "smtp_password": self.config.get("smtp_password"),
                "smtp_use_tls": self.config.get("smtp_use_tls", True),
                "from_email": self.config.get("from_email"),
                "from_name": self.config.get("from_name", "Chronicle AI"),
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

        logger.info("✅ Email Summarizer plugin initialized successfully")

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
        logger.info("Email Summarizer plugin cleanup complete")

    async def on_conversation_complete(
        self, context: PluginContext
    ) -> Optional[PluginResult]:
        """
        Send email summary after all conversation processing completes.

        This fires after speaker recognition, memory extraction, and title/summary
        generation have all finished, so the conversation has its final title,
        summary (with speaker names), and detailed summary.

        Args:
            context: Plugin context with conversation.complete event data
                - conversation_id: str
                - conversation: dict with client_id, user_id
                - transcript: str
                - duration: float
        """
        try:
            conversation_id = context.data.get("conversation_id", "unknown")
            logger.info(
                f"Processing conversation.complete event for user: {context.user_id}, "
                f"conversation: {conversation_id}"
            )

            # Fetch full conversation via Beanie model (has title, summary, transcript by now)
            conversation = await Conversation.find_one(
                Conversation.conversation_id == conversation_id
            )
            if not conversation:
                logger.warning(
                    f"Conversation {conversation_id} not found in DB, skipping email"
                )
                return PluginResult(success=False, message="Conversation not found")

            # Get transcript from active version (computed property handles version lookup)
            transcript = conversation.transcript
            if not transcript:
                logger.warning(
                    f"No transcript for conversation {conversation_id}, skipping email"
                )
                return PluginResult(success=False, message="Skipped: Empty transcript")

            # Use the DB summary (already generated by this point)
            summary = conversation.detailed_summary or conversation.summary
            if not summary:
                logger.warning(
                    f"No summary for conversation {conversation_id}, skipping email"
                )
                return PluginResult(
                    success=False, message="Skipped: No summary available"
                )

            title = conversation.title or self.subject_prefix

            # Send to the configured SMTP username (the user's own email)
            user_email = self.config.get("smtp_username")
            if not user_email:
                return PluginResult(
                    success=False,
                    message="No smtp_username configured for email delivery",
                )

            # Format and send
            created_at = conversation.created_at
            duration = conversation.audio_total_duration or 0

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
                logger.info(
                    f"✅ Email summary sent to {user_email} for conversation {conversation_id}"
                )
                return PluginResult(
                    success=True,
                    message=f"Email sent to {user_email}",
                    data={
                        "recipient": user_email,
                        "conversation_id": conversation_id,
                        "title": title,
                    },
                )
            else:
                logger.error(f"Failed to send email to {user_email}")
                return PluginResult(
                    success=False, message=f"Failed to send email to {user_email}"
                )

        except Exception as e:
            logger.error(
                f"Error in email summarizer (conversation.complete): {e}", exc_info=True
            )
            return PluginResult(success=False, message=f"Error: {str(e)}")

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
            required_fields = [
                "smtp_host",
                "smtp_username",
                "smtp_password",
                "from_email",
            ]
            missing_fields = [
                field for field in required_fields if not config.get(field)
            ]

            if missing_fields:
                return {
                    "success": False,
                    "message": f"Missing required fields: {', '.join(missing_fields)}",
                    "status": "error",
                }

            # Build SMTP config
            smtp_config = {
                "smtp_host": config.get("smtp_host"),
                "smtp_port": config.get("smtp_port", 587),
                "smtp_username": config.get("smtp_username"),
                "smtp_password": config.get("smtp_password"),
                "smtp_use_tls": config.get("smtp_use_tls", True),
                "from_email": config.get("from_email"),
                "from_name": config.get("from_name", "Chronicle AI"),
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
            error_msg = str(e)

            # Provide helpful hints based on error type
            hints = []
            if "Authentication" in error_msg or "535" in error_msg:
                hints.append(
                    "For Gmail: Enable 2FA and create an App Password at https://myaccount.google.com/apppasswords"
                )
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
                "hints": hints,
            }
