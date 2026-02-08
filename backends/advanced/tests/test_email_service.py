"""
Unit tests for the SMTP Email Service.

Tests email service initialization, configuration validation, and sending functionality.
"""
import pytest
from unittest.mock import Mock, patch, MagicMock
from advanced_omi_backend.plugins.email_summarizer.email_service import SMTPEmailService


class TestSMTPEmailService:
    """Test SMTP Email Service."""

    def test_initialization_with_valid_config(self):
        """Test that service initializes with valid configuration."""
        config = {
            'smtp_host': 'smtp.gmail.com',
            'smtp_port': 587,
            'smtp_username': 'test@example.com',
            'smtp_password': 'test_password',
            'smtp_use_tls': True,
            'from_email': 'noreply@chronicle.ai',
            'from_name': 'Chronicle AI',
        }

        service = SMTPEmailService(config)

        assert service.host == 'smtp.gmail.com'
        assert service.port == 587
        assert service.username == 'test@example.com'
        assert service.password == 'test_password'
        assert service.use_tls is True
        assert service.from_email == 'noreply@chronicle.ai'
        assert service.from_name == 'Chronicle AI'

    def test_initialization_with_missing_required_fields(self):
        """Test that service raises ValueError with incomplete config."""
        incomplete_configs = [
            {
                # Missing smtp_host
                'smtp_username': 'test@example.com',
                'smtp_password': 'password',
                'from_email': 'test@example.com',
            },
            {
                # Missing smtp_username
                'smtp_host': 'smtp.gmail.com',
                'smtp_password': 'password',
                'from_email': 'test@example.com',
            },
            {
                # Missing smtp_password
                'smtp_host': 'smtp.gmail.com',
                'smtp_username': 'test@example.com',
                'from_email': 'test@example.com',
            },
            {
                # Missing from_email
                'smtp_host': 'smtp.gmail.com',
                'smtp_username': 'test@example.com',
                'smtp_password': 'password',
            },
        ]

        for config in incomplete_configs:
            with pytest.raises(ValueError, match="SMTP configuration incomplete"):
                SMTPEmailService(config)

    def test_initialization_with_defaults(self):
        """Test that service uses default values for optional fields."""
        config = {
            'smtp_host': 'smtp.gmail.com',
            'smtp_username': 'test@example.com',
            'smtp_password': 'password',
            'from_email': 'test@example.com',
            # No smtp_port, smtp_use_tls, from_name
        }

        service = SMTPEmailService(config)

        assert service.port == 587  # Default port
        assert service.use_tls is True  # Default TLS
        assert service.from_name == 'Chronicle AI'  # Default name

    @pytest.mark.asyncio
    async def test_send_email_text_only(self):
        """Test sending plain text email."""
        config = {
            'smtp_host': 'smtp.gmail.com',
            'smtp_port': 587,
            'smtp_username': 'test@example.com',
            'smtp_password': 'password',
            'smtp_use_tls': True,
            'from_email': 'noreply@chronicle.ai',
            'from_name': 'Chronicle AI',
        }

        service = SMTPEmailService(config)

        # Mock the SMTP sending
        with patch.object(service, '_send_smtp') as mock_send:
            result = await service.send_email(
                to_email='recipient@example.com',
                subject='Test Subject',
                body_text='This is a test email.',
            )

            assert result is True
            assert mock_send.called
            # Check that MIME message was created
            msg = mock_send.call_args[0][0]
            assert msg['Subject'] == 'Test Subject'
            assert msg['To'] == 'recipient@example.com'
            assert 'Chronicle AI' in msg['From']

    @pytest.mark.asyncio
    async def test_send_email_with_html(self):
        """Test sending email with HTML and plain text versions."""
        config = {
            'smtp_host': 'smtp.gmail.com',
            'smtp_port': 587,
            'smtp_username': 'test@example.com',
            'smtp_password': 'password',
            'smtp_use_tls': True,
            'from_email': 'noreply@chronicle.ai',
            'from_name': 'Chronicle AI',
        }

        service = SMTPEmailService(config)

        # Mock the SMTP sending
        with patch.object(service, '_send_smtp') as mock_send:
            result = await service.send_email(
                to_email='recipient@example.com',
                subject='Test Subject',
                body_text='Plain text version',
                body_html='<h1>HTML version</h1>',
            )

            assert result is True
            assert mock_send.called

            # Check that both plain text and HTML parts exist
            msg = mock_send.call_args[0][0]
            assert msg.is_multipart()

    @pytest.mark.asyncio
    async def test_send_email_failure_returns_false(self):
        """Test that send_email returns False on failure."""
        config = {
            'smtp_host': 'smtp.gmail.com',
            'smtp_port': 587,
            'smtp_username': 'test@example.com',
            'smtp_password': 'password',
            'smtp_use_tls': True,
            'from_email': 'noreply@chronicle.ai',
            'from_name': 'Chronicle AI',
        }

        service = SMTPEmailService(config)

        # Mock the SMTP sending to raise an exception
        with patch.object(service, '_send_smtp', side_effect=Exception("SMTP error")):
            result = await service.send_email(
                to_email='recipient@example.com',
                subject='Test Subject',
                body_text='This should fail',
            )

            assert result is False

    @pytest.mark.asyncio
    async def test_connection_test_success(self):
        """Test successful SMTP connection test."""
        config = {
            'smtp_host': 'smtp.gmail.com',
            'smtp_port': 587,
            'smtp_username': 'test@example.com',
            'smtp_password': 'password',
            'smtp_use_tls': True,
            'from_email': 'noreply@chronicle.ai',
            'from_name': 'Chronicle AI',
        }

        service = SMTPEmailService(config)

        # Mock the connection test
        with patch.object(service, '_test_smtp_connection'):
            result = await service.test_connection()
            assert result is True

    @pytest.mark.asyncio
    async def test_connection_test_failure(self):
        """Test failed SMTP connection test."""
        config = {
            'smtp_host': 'smtp.gmail.com',
            'smtp_port': 587,
            'smtp_username': 'test@example.com',
            'smtp_password': 'password',
            'smtp_use_tls': True,
            'from_email': 'noreply@chronicle.ai',
            'from_name': 'Chronicle AI',
        }

        service = SMTPEmailService(config)

        # Mock the connection test to fail
        with patch.object(service, '_test_smtp_connection', side_effect=Exception("Connection failed")):
            result = await service.test_connection()
            assert result is False
