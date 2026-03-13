#!/usr/bin/env python3
"""
Script to delete all conversations using the API endpoints.
This uses the proper API authentication and endpoints.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

# Load environment variables
load_dotenv(Path(__file__).parent.parent / ".env")


async def get_auth_token():
    """Get admin authentication token."""
    admin_email = os.getenv("ADMIN_EMAIL")
    admin_password = os.getenv("ADMIN_PASSWORD")

    if not admin_email or not admin_password:
        print("Error: ADMIN_EMAIL and ADMIN_PASSWORD must be set in .env file")
        sys.exit(1)

    base_url = "http://localhost:8000"

    async with aiohttp.ClientSession() as session:
        # Login to get token
        login_data = {"username": admin_email, "password": admin_password}

        async with session.post(
            f"{base_url}/auth/jwt/login", data=login_data
        ) as response:
            if response.status != 200:
                print(f"Failed to login: {response.status}")
                text = await response.text()
                print(f"Response: {text}")
                sys.exit(1)

            result = await response.json()
            return result["access_token"]


async def delete_all_conversations(skip_prompt=False):
    """Delete all conversations using the API."""

    base_url = "http://localhost:8000"

    # Get auth token
    print("Getting admin authentication token...")
    token = await get_auth_token()

    headers = {"Authorization": f"Bearer {token}"}

    async with aiohttp.ClientSession() as session:
        # First, get all conversations
        print("Fetching all conversations...")
        async with session.get(
            f"{base_url}/api/conversations", headers=headers
        ) as response:
            if response.status != 200:
                print(f"Failed to fetch conversations: {response.status}")
                text = await response.text()
                print(f"Response: {text}")
                return

            data = await response.json()

        # Extract conversations from nested structure
        conversations_dict = data.get("conversations", {})
        conversations = []
        for client_id, client_conversations in conversations_dict.items():
            conversations.extend(client_conversations)

        print(f"Found {len(conversations)} conversations")

        if len(conversations) == 0:
            print("No conversations to delete")
            return

        # Confirm deletion unless --yes flag is used
        if not skip_prompt:
            response = input(
                f"Are you sure you want to delete ALL {len(conversations)} conversations? (yes/no): "
            )
            if response.lower() != "yes":
                print("Deletion cancelled")
                return

        # Delete each conversation
        deleted_count = 0
        failed_count = 0

        for conv in conversations:
            audio_uuid = conv.get("audio_uuid")
            if not audio_uuid:
                print(f"Skipping conversation without audio_uuid: {conv.get('_id')}")
                continue

            # Delete the conversation
            async with session.delete(
                f"{base_url}/api/conversations/{audio_uuid}", headers=headers
            ) as response:
                if response.status == 200:
                    deleted_count += 1
                    print(
                        f"Deleted conversation {audio_uuid} ({deleted_count}/{len(conversations)})"
                    )
                else:
                    failed_count += 1
                    text = await response.text()
                    print(f"Failed to delete {audio_uuid}: {response.status} - {text}")

        print(f"\nDeletion complete:")
        print(f"  Successfully deleted: {deleted_count}")
        print(f"  Failed: {failed_count}")


if __name__ == "__main__":
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Delete all conversations")
    parser.add_argument(
        "--yes", "-y", action="store_true", help="Skip confirmation prompt"
    )
    args = parser.parse_args()

    asyncio.run(delete_all_conversations(skip_prompt=args.yes))
