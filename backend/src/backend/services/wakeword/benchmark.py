"""CLI report for the latest durable wake interaction latency traces."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

from .interaction_ledger import WakeAudioInterval, WakeInteractionFact
from .latency import build_wake_latency_report

COLLECTION = "wake_interaction_facts"


def _fact_from_document(document: dict[str, Any]) -> WakeInteractionFact:
    values = {key: value for key, value in document.items() if key != "_id"}
    interval = values.get("audio_interval")
    if interval:
        values["audio_interval"] = WakeAudioInterval(**interval)
    values.setdefault("payload", {})
    return WakeInteractionFact(**values)


async def _latest_trace_ids(collection, *, client_id: str | None, limit: int):
    match = {"client_id": client_id} if client_id else {}
    pipeline = [
        {"$match": match},
        {
            "$group": {
                "_id": "$wake_trace_id",
                "latest": {"$max": "$occurred_at"},
            }
        },
        {"$sort": {"latest": -1}},
        {"$limit": limit},
    ]
    return [row["_id"] async for row in collection.aggregate(pipeline)]


async def _run(args) -> list[dict[str, Any]]:
    client = AsyncIOMotorClient(
        os.getenv("MONGODB_URI", "mongodb://mongo:27017"),
        serverSelectionTimeoutMS=5000,
        tz_aware=True,
    )
    try:
        database = client[os.getenv("MONGODB_DATABASE", "chronicle")]
        collection = database[COLLECTION]
        trace_ids = (
            [args.trace_id]
            if args.trace_id
            else await _latest_trace_ids(
                collection, client_id=args.client_id, limit=args.limit
            )
        )
        reports = []
        for trace_id in trace_ids:
            documents = (
                await collection.find({"wake_trace_id": trace_id})
                .sort([("occurred_at", 1), ("ordinal", 1)])
                .to_list(length=None)
            )
            if documents:
                reports.append(
                    build_wake_latency_report(
                        _fact_from_document(document) for document in documents
                    ).to_dict()
                )
        return reports
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Report stage-by-stage latency for durable wake traces"
    )
    parser.add_argument("--trace-id")
    parser.add_argument("--client-id")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    if args.limit <= 0:
        parser.error("--limit must be positive")
    print(json.dumps(asyncio.run(_run(args)), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
