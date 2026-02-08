"""
SMTP Email Service for Chronicle.

Provides email sending functionality via SMTP protocol with support for:
- HTML and plain text emails
- TLS/SSL encryption
- Gmail and other SMTP providers
- Async implementation
"""
import asyncio
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, Optional

from advanced_omi_backend.utils.logging_utils import mask_dict

logger = logging.getLogger(__name__)


class SMTPEmailService:
    """SMTP email service for sending emails via SMTP protocol."""

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize SMTP email service with configuration.

        Args:
            config: SMTP configuration containing:
                - smtp_host: SMTP server hostname
                - smtp_port: SMTP server port (default: 587)
                - smtp_username: SMTP username
                - smtp_password: SMTP password
                - smtp_use_tls: Whether to use TLS (default: True)
                - from_email: Sender email address
                - from_name: Sender display name (default: 'Chronicle AI')
        """
        self.host = config.get('smtp_host')
        self.port = config.get('smtp_port', 587)
        self.username = config.get('smtp_username')
        self.password = config.get('smtp_password')
        self.use_tls = config.get('smtp_use_tls', True)
        self.from_email = config.get('from_email')
        self.from_name = config.get('from_name', 'Chronicle AI')

        # Validate required configuration
        if not all([self.host, self.username, self.password, self.from_email]):
            raise ValueError(
                "SMTP configuration incomplete. Required: smtp_host, smtp_username, "
                "smtp_password, from_email"
            )

        # Log configuration with masked secrets
        masked_config = mask_dict(config)
        logger.info(
            f"SMTP Email Service initialized: {self.username}@{self.host}:{self.port} "
            f"(TLS: {self.use_tls})"
        )
        logger.debug(f"SMTP config: {masked_config}")

    async def send_email(
        self,
        to_email: str,
        subject: str,
        body_text: str,
        body_html: Optional[str] = None
    ) -> bool:
        """
        Send email via SMTP with HTML/text support.

        Args:
            to_email: Recipient email address
            subject: Email subject line
            body_text: Plain text email body
            body_html: Optional HTML email body

        Returns:
            True if email sent successfully, False otherwise
        """
        try:
            # Create message container
            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = f"{self.from_name} <{self.from_email}>"
            msg['To'] = to_email

            # Attach plain text version
            text_part = MIMEText(body_text, 'plain')
            msg.attach(text_part)

            # Attach HTML version if provided
            if body_html:
                html_part = MIMEText(body_html, 'html')
                msg.attach(html_part)

            # Send email asynchronously (run in thread pool to avoid blocking)
            await asyncio.to_thread(self._send_smtp, msg, to_email)

            logger.info(f"✅ Email sent successfully to {to_email}: {subject}")
            return True

        except Exception as e:
            logger.error(f"Failed to send email to {to_email}: {e}", exc_info=True)
            return False

    def _send_smtp(self, msg: MIMEMultipart, to_email: str) -> None:
        """
        Internal method to send email via SMTP (blocking).

        Args:
            msg: MIME message to send
            to_email: Recipient email address

        Raises:
            Exception: If SMTP sending fails
        """
        # Connect to SMTP server
        if self.use_tls:
            # Use STARTTLS (most common for port 587)
            smtp_server = smtplib.SMTP(self.host, self.port, timeout=30)
            smtp_server.ehlo()
            smtp_server.starttls()
            smtp_server.ehlo()
        else:
            # Direct connection (for port 465 SSL or no encryption)
            smtp_server = smtplib.SMTP(self.host, self.port, timeout=30)

        try:
            # Login and send
            smtp_server.login(self.username, self.password)
            smtp_server.send_message(msg)
            logger.debug(f"SMTP send completed for {to_email}")
        finally:
            smtp_server.quit()

    async def test_connection(self) -> bool:
        """
        Test SMTP connectivity and authentication.

        Returns:
            True if connection successful, False otherwise
        """
        try:
            await asyncio.to_thread(self._test_smtp_connection)
            logger.info(f"✅ SMTP connection test successful: {self.username}@{self.host}")
            return True
        except Exception as e:
            logger.error(f"SMTP connection test failed: {e}", exc_info=True)
            return False

    def _test_smtp_connection(self) -> None:
        """
        Internal method to test SMTP connection (blocking).

        Raises:
            Exception: If connection fails
        """
        try:
            if self.use_tls:
                smtp_server = smtplib.SMTP(self.host, self.port, timeout=10)
                smtp_server.ehlo()
                smtp_server.starttls()
                smtp_server.ehlo()
            else:
                smtp_server = smtplib.SMTP(self.host, self.port, timeout=10)

            try:
                smtp_server.login(self.username, self.password)
                logger.debug("SMTP authentication successful")
            finally:
                smtp_server.quit()
        except smtplib.SMTPAuthenticationError as e:
            # Note: Error message from smtplib should not contain password, but be cautious
            raise Exception(f"SMTP Authentication failed for {self.username}. Check credentials. For Gmail, use an App Password instead of your regular password. Error: {str(e)}")
        except smtplib.SMTPConnectError as e:
            raise Exception(f"Failed to connect to SMTP server {self.host}:{self.port}. Check host and port. Error: {str(e)}")
        except smtplib.SMTPServerDisconnected as e:
            raise Exception(f"SMTP server disconnected unexpectedly. Check TLS settings (port 587 needs TLS, port 465 needs SSL). Error: {str(e)}")
        except TimeoutError as e:
            raise Exception(f"Connection to {self.host}:{self.port} timed out. Check firewall/network settings. Error: {str(e)}")
        except Exception as e:
            raise Exception(f"SMTP connection test failed: {type(e).__name__}: {str(e)}")


# Test script for development/debugging
async def main():
    """Test the SMTP email service."""
    import os

    from dotenv import load_dotenv

    load_dotenv()

    config = {
        'smtp_host': os.getenv('SMTP_HOST', 'smtp.gmail.com'),
        'smtp_port': int(os.getenv('SMTP_PORT', 587)),
        'smtp_username': os.getenv('SMTP_USERNAME'),
        'smtp_password': os.getenv('SMTP_PASSWORD'),
        'smtp_use_tls': os.getenv('SMTP_USE_TLS', 'true').lower() == 'true',
        'from_email': os.getenv('FROM_EMAIL', 'noreply@chronicle.ai'),
        'from_name': os.getenv('FROM_NAME', 'Chronicle AI'),
    }

    try:
        service = SMTPEmailService(config)

        # Test connection
        print("Testing SMTP connection...")
        if await service.test_connection():
            print("✅ Connection test passed")
        else:
            print("❌ Connection test failed")
            return

        # Send test email
        test_email = config['smtp_username']  # Send to self
        print(f"\nSending test email to {test_email}...")

        success = await service.send_email(
            to_email=test_email,
            subject="Chronicle Email Service Test",
            body_text="This is a test email from Chronicle Email Service.\n\nIf you received this, the email service is working correctly!",
            body_html="<h2>Chronicle Email Service Test</h2><p>This is a test email from Chronicle Email Service.</p><p>If you received this, the email service is working correctly!</p>"
        )

        if success:
            print("✅ Test email sent successfully")
        else:
            print("❌ Failed to send test email")

    except Exception as e:
        print(f"❌ Error: {e}")


if __name__ == '__main__':
    asyncio.run(main())
