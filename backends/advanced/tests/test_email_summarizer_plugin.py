"""
Unit tests for the Email Summarizer Plugin.

Tests plugin initialization, configuration, and event handling.
"""
import pytest
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime

from advanced_omi_backend.plugins.email_summarizer.plugin import EmailSummarizerPlugin
from advanced_omi_backend.plugins.base import PluginContext, PluginResult


class TestEmailSummarizerPlugin:
    """Test Email Summarizer Plugin."""

    def test_plugin_initialization(self):
        """Test that plugin initializes with valid configuration."""
        config = {
            'enabled': True,
            'events': ['conversation.complete'],
            'condition': {'type': 'always'},
            'smtp_host': 'smtp.gmail.com',
            'smtp_port': 587,
            'smtp_username': 'test@example.com',
            'smtp_password': 'password',
            'smtp_use_tls': True,
            'from_email': 'noreply@chronicle.ai',
            'from_name': 'Chronicle AI',
            'subject_prefix': 'Conversation Summary',
            'summary_max_sentences': 3,
        }

        plugin = EmailSummarizerPlugin(config)

        assert plugin.enabled is True
        assert plugin.subject_prefix == 'Conversation Summary'
        assert plugin.summary_max_sentences == 3
        assert plugin.include_conversation_id is True
        assert plugin.include_duration is True

    def test_plugin_uses_defaults_for_optional_fields(self):
        """Test that plugin uses default values for optional configuration."""
        config = {
            'enabled': True,
            'events': ['conversation.complete'],
            'smtp_host': 'smtp.gmail.com',
            'smtp_username': 'test@example.com',
            'smtp_password': 'password',
            'from_email': 'noreply@chronicle.ai',
        }

        plugin = EmailSummarizerPlugin(config)

        assert plugin.subject_prefix == 'Conversation Summary'  # Default
        assert plugin.summary_max_sentences == 3  # Default
        assert plugin.include_conversation_id is True  # Default
        assert plugin.include_duration is True  # Default

    @pytest.mark.asyncio
    async def test_plugin_skips_empty_transcript(self):
        """Test that plugin skips conversations with empty transcripts."""
        config = {
            'enabled': True,
            'events': ['conversation.complete'],
            'smtp_host': 'smtp.gmail.com',
            'smtp_username': 'test@example.com',
            'smtp_password': 'password',
            'from_email': 'noreply@chronicle.ai',
        }

        plugin = EmailSummarizerPlugin(config)

        # Mock the email service (not initialized yet, but that's OK for this test)
        plugin.email_service = Mock()

        # Create context with empty transcript
        context = PluginContext(
            user_id='test-user',
            event='conversation.complete',
            data={
                'conversation': {},
                'transcript': '',  # Empty transcript
                'duration': 0,
                'conversation_id': 'test-conv',
            }
        )

        result = await plugin.on_conversation_complete(context)

        assert result.success is False
        assert 'Empty transcript' in result.message

    @pytest.mark.asyncio
    async def test_plugin_handles_missing_user_email(self):
        """Test that plugin handles missing user email gracefully."""
        config = {
            'enabled': True,
            'events': ['conversation.complete'],
            'smtp_host': 'smtp.gmail.com',
            'smtp_username': 'test@example.com',
            'smtp_password': 'password',
            'from_email': 'noreply@chronicle.ai',
        }

        plugin = EmailSummarizerPlugin(config)

        # Mock _get_user_email to return None
        plugin._get_user_email = AsyncMock(return_value=None)

        context = PluginContext(
            user_id='test-user',
            event='conversation.complete',
            data={
                'conversation': {},
                'transcript': 'Test conversation',
                'duration': 60,
                'conversation_id': 'test-conv',
            }
        )

        result = await plugin.on_conversation_complete(context)

        assert result.success is False
        assert 'No email' in result.message

    @pytest.mark.asyncio
    async def test_plugin_sends_email_on_successful_processing(self):
        """Test that plugin sends email when everything is configured correctly."""
        config = {
            'enabled': True,
            'events': ['conversation.complete'],
            'smtp_host': 'smtp.gmail.com',
            'smtp_username': 'test@example.com',
            'smtp_password': 'password',
            'from_email': 'noreply@chronicle.ai',
        }

        plugin = EmailSummarizerPlugin(config)

        # Mock dependencies
        plugin._get_user_email = AsyncMock(return_value='user@example.com')
        plugin._generate_summary = AsyncMock(return_value='This is a test summary.')
        plugin.email_service = AsyncMock()
        plugin.email_service.send_email = AsyncMock(return_value=True)

        context = PluginContext(
            user_id='test-user',
            event='conversation.complete',
            data={
                'conversation': {'created_at': datetime.now()},
                'transcript': 'This is a test conversation with meaningful content.',
                'duration': 120,
                'conversation_id': 'test-conv-123',
            }
        )

        result = await plugin.on_conversation_complete(context)

        assert result.success is True
        assert 'Email sent' in result.message
        assert result.data['recipient'] == 'user@example.com'

        # Verify email was sent
        plugin.email_service.send_email.assert_called_once()
        call_args = plugin.email_service.send_email.call_args[1]
        assert call_args['to_email'] == 'user@example.com'
        assert 'Conversation Summary' in call_args['subject']

    @pytest.mark.asyncio
    async def test_plugin_handles_llm_failure_gracefully(self):
        """Test that plugin falls back to truncated transcript if LLM fails."""
        config = {
            'enabled': True,
            'events': ['conversation.complete'],
            'smtp_host': 'smtp.gmail.com',
            'smtp_username': 'test@example.com',
            'smtp_password': 'password',
            'from_email': 'noreply@chronicle.ai',
        }

        plugin = EmailSummarizerPlugin(config)

        # Mock dependencies
        plugin._get_user_email = AsyncMock(return_value='user@example.com')
        plugin.email_service = AsyncMock()
        plugin.email_service.send_email = AsyncMock(return_value=True)

        # Mock LLM to fail
        with patch('advanced_omi_backend.plugins.email_summarizer.plugin.async_generate') as mock_llm:
            mock_llm.side_effect = Exception("LLM service unavailable")

            context = PluginContext(
                user_id='test-user',
                event='conversation.complete',
                data={
                    'conversation': {},
                    'transcript': 'A' * 400,  # Long transcript
                    'duration': 60,
                    'conversation_id': 'test-conv',
                }
            )

            result = await plugin.on_conversation_complete(context)

            # Should still succeed (fallback to truncated transcript)
            assert result.success is True

            # Verify email was sent with truncated transcript
            plugin.email_service.send_email.assert_called_once()

    @pytest.mark.asyncio
    async def test_plugin_handles_email_send_failure(self):
        """Test that plugin reports failure when email sending fails."""
        config = {
            'enabled': True,
            'events': ['conversation.complete'],
            'smtp_host': 'smtp.gmail.com',
            'smtp_username': 'test@example.com',
            'smtp_password': 'password',
            'from_email': 'noreply@chronicle.ai',
        }

        plugin = EmailSummarizerPlugin(config)

        # Mock dependencies
        plugin._get_user_email = AsyncMock(return_value='user@example.com')
        plugin._generate_summary = AsyncMock(return_value='Test summary')
        plugin.email_service = AsyncMock()
        plugin.email_service.send_email = AsyncMock(return_value=False)  # Email fails

        context = PluginContext(
            user_id='test-user',
            event='conversation.complete',
            data={
                'conversation': {},
                'transcript': 'Test conversation',
                'duration': 60,
                'conversation_id': 'test-conv',
            }
        )

        result = await plugin.on_conversation_complete(context)

        assert result.success is False
        assert 'Failed to send email' in result.message

    def test_format_subject_with_timestamp(self):
        """Test email subject formatting with timestamp."""
        config = {
            'enabled': True,
            'events': ['conversation.complete'],
            'smtp_host': 'smtp.gmail.com',
            'smtp_username': 'test@example.com',
            'smtp_password': 'password',
            'from_email': 'noreply@chronicle.ai',
            'subject_prefix': 'Test Summary',
        }

        plugin = EmailSummarizerPlugin(config)

        created_at = datetime(2025, 1, 15, 14, 30, 0)
        subject = plugin._format_subject(created_at)

        assert 'Test Summary' in subject
        assert 'Jan 15, 2025' in subject

    def test_format_subject_without_timestamp(self):
        """Test email subject formatting without timestamp."""
        config = {
            'enabled': True,
            'events': ['conversation.complete'],
            'smtp_host': 'smtp.gmail.com',
            'smtp_username': 'test@example.com',
            'smtp_password': 'password',
            'from_email': 'noreply@chronicle.ai',
            'subject_prefix': 'Conversation Summary',
        }

        plugin = EmailSummarizerPlugin(config)

        subject = plugin._format_subject(None)

        assert subject == 'Conversation Summary'
