"""
Annotation cron scheduler for AI-powered suggestion surfacing.

This scheduler runs background jobs to:
1. Surface AI suggestions for potential transcript/memory errors (daily)
2. Fine-tune error detection models using user feedback (weekly)

Configuration via environment variables:
- MONGODB_URI: MongoDB connection string
- DEV_MODE: When true, uses 1-minute intervals for testing

Usage:
    uv run python -m advanced_omi_backend.cron
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from advanced_omi_backend.models.annotation import Annotation
from advanced_omi_backend.models.conversation import Conversation
from advanced_omi_backend.models.user import User
from advanced_omi_backend.workers.annotation_jobs import (
    finetune_hallucination_model,
    surface_error_suggestions,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Configuration
MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://mongo:27017")
DEV_MODE = os.getenv("DEV_MODE", "false").lower() == "true"

# Intervals (1 minute in dev, normal in production)
if DEV_MODE:
    SUGGESTION_INTERVAL = 60  # 1 minute for dev testing
    TRAINING_INTERVAL = 60  # 1 minute for dev testing
    logger.info("🔧 DEV_MODE enabled - using 1-minute intervals for testing")
else:
    SUGGESTION_INTERVAL = 24 * 60 * 60  # Daily
    TRAINING_INTERVAL = 7 * 24 * 60 * 60  # Weekly
    logger.info("📅 Production mode - using daily/weekly intervals")


async def init_db():
    """Initialize database connection"""
    try:
        client = AsyncIOMotorClient(MONGODB_URI)
        await init_beanie(
            database=client.chronicle,
            document_models=[Annotation, Conversation, User],
        )
        logger.info("✅ Database connection initialized")
    except Exception as e:
        logger.error(f"❌ Failed to initialize database: {e}")
        raise


async def run_scheduler():
    """Main scheduler loop"""
    await init_db()
    logger.info("🕐 Annotation cron scheduler started")
    logger.info(f"   - Suggestion interval: {SUGGESTION_INTERVAL}s")
    logger.info(f"   - Training interval: {TRAINING_INTERVAL}s")

    last_suggestion_run = datetime.now(timezone.utc)
    last_training_run = datetime.now(timezone.utc)

    while True:
        try:
            now = datetime.now(timezone.utc)

            # Daily: Surface AI suggestions
            if (now - last_suggestion_run).total_seconds() >= SUGGESTION_INTERVAL:
                logger.info(f"🤖 Running suggestion surfacing at {now}")
                try:
                    await surface_error_suggestions()
                    last_suggestion_run = now
                    logger.info("✅ Suggestion surfacing completed")
                except Exception as e:
                    logger.error(f"❌ Suggestion job failed: {e}", exc_info=True)

            # Weekly: Fine-tune model
            if (now - last_training_run).total_seconds() >= TRAINING_INTERVAL:
                logger.info(f"🎓 Running model fine-tuning at {now}")
                try:
                    await finetune_hallucination_model()
                    last_training_run = now
                    logger.info("✅ Model fine-tuning completed")
                except Exception as e:
                    logger.error(f"❌ Training job failed: {e}", exc_info=True)

            # Sleep for check interval
            await asyncio.sleep(60)  # Check every minute

        except KeyboardInterrupt:
            logger.info("⛔ Scheduler stopped by user")
            break
        except Exception as e:
            logger.error(f"❌ Unexpected error in scheduler loop: {e}", exc_info=True)
            # Continue running despite errors
            await asyncio.sleep(60)


if __name__ == "__main__":
    logger.info("🚀 Starting annotation cron scheduler...")
    try:
        asyncio.run(run_scheduler())
    except KeyboardInterrupt:
        logger.info("👋 Annotation cron scheduler stopped")
    except Exception as e:
        logger.error(f"💥 Fatal error: {e}", exc_info=True)
        exit(1)
