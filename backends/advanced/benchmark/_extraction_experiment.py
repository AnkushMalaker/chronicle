"""Isolated extraction experiment for one LongMemEval answer session.

Runs only the answer session for ``QID 5d3d2817`` through Graphiti several
ways and inspects the extracted RELATES_TO edges. Each variant lands in its
own FalkorDB graph so they don't contaminate each other.

The baseline matches what `chat_service.extract_memories_from_session`
sends today: one big "User: ...\\nAssistant: ..." message episode.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

from advanced_omi_backend.services.graph_client import GraphClient
from advanced_omi_backend.services.memory.config import (
    MemoryConfig,
    MemoryProvider,
    build_memory_config_from_env,
)
from advanced_omi_backend.services.memory.providers.graphiti import (
    GraphitiMemoryService,
)


DATASET_PATH = (
    "/root/.cache/huggingface/hub/datasets--xiaowu0162--longmemeval-cleaned/"
    "snapshots/98d7416c24c778c2fee6e6f3006e7a073259d48f/longmemeval_s_cleaned.json"
)
QID = "5d3d2817"


def _load_answer_session() -> tuple[list[dict[str, str]], datetime]:
    with open(DATASET_PATH) as f:
        data = json.load(f)
    row = next(r for r in data if r["question_id"] == QID)
    answer_ids = set(row["answer_session_ids"])
    for i, sid in enumerate(row["haystack_session_ids"]):
        if sid in answer_ids:
            session = row["haystack_sessions"][i]
            date_str = row["haystack_dates"][i]
            try:
                ts = datetime.strptime(date_str.split(" (")[0], "%Y/%m/%d")
            except ValueError:
                ts = datetime.now(timezone.utc).replace(tzinfo=None)
            return session, ts.replace(tzinfo=timezone.utc)
    raise SystemExit(f"answer session not found for {QID}")


async def _drop_graph(graph_name: str) -> None:
    client = GraphClient(
        host=os.getenv("FALKORDB_HOST", "falkordb"),
        port=int(os.getenv("FALKORDB_PORT", "6379")),
        graph_name=graph_name,
    )
    try:
        await asyncio.to_thread(client.delete_graph)
    except Exception:
        pass
    finally:
        client.close()


async def _make_service() -> GraphitiMemoryService:
    cfg = build_memory_config_from_env()
    if cfg.memory_provider != MemoryProvider.GRAPHITI:
        raise SystemExit(
            f"Expected MEMORY_PROVIDER=graphiti, got {cfg.memory_provider.value}"
        )
    svc = GraphitiMemoryService(cfg)
    await svc.initialize()
    return svc


async def _list_facts(graph_name: str) -> list[dict[str, Any]]:
    client = GraphClient(
        host=os.getenv("FALKORDB_HOST", "falkordb"),
        port=int(os.getenv("FALKORDB_PORT", "6379")),
        graph_name=graph_name,
    )
    try:
        rows = await asyncio.to_thread(
            lambda: client.session().run(
                "MATCH (s:Entity)-[r:RELATES_TO]->(t:Entity) "
                "RETURN s.name AS source, r.name AS relation, "
                "t.name AS target, r.fact AS fact"
            )
        )
        return rows or []
    finally:
        client.close()


def _print_facts(label: str, facts: list[dict[str, Any]]) -> None:
    print(f"\n=== {label}: {len(facts)} facts ===", flush=True)
    for f in facts:
        print(
            f"  ({f['source']}) -[{f['relation']}]-> ({f['target']})",
            flush=True,
        )
        print(f"    {f['fact']}", flush=True)
    hits = [
        f
        for f in facts
        if any(
            kw in (f.get("fact") or "").lower()
            for kw in ("marketing", "specialist", "previous", "startup")
        )
    ]
    print(
        f"  >> {len(hits)} hit(s) for marketing/specialist/previous/startup",
        flush=True,
    )
    for f in hits:
        print(f"     ★ {f['fact']}", flush=True)


async def _run_variant_full_message(
    svc: GraphitiMemoryService,
    user_id: str,
    session: list[dict[str, str]],
    ts: datetime,
) -> str:
    """Variant A: single message episode containing all turns (current path)."""
    transcript_parts = []
    prefix = f"[{ts.strftime('%Y-%m-%d %H:%M')}] "
    for turn in session:
        role = "User" if turn["role"] == "user" else "Assistant"
        transcript_parts.append(f"{role}: {prefix}{turn['content']}")
    transcript = "\n".join(transcript_parts)

    _, group_id = await svc._get_graphiti(user_id)
    await svc.add_memory(
        transcript=transcript,
        client_id="experiment",
        source_id="exp_full_message",
        user_id=user_id,
        user_email="exp@example.com",
        allow_update=False,
    )
    return group_id


async def _run_variant_per_turn(
    svc: GraphitiMemoryService,
    user_id: str,
    session: list[dict[str, str]],
    ts: datetime,
) -> str:
    """Variant B: one episode per turn, both user and assistant."""
    _, group_id = await svc._get_graphiti(user_id)
    prefix = f"[{ts.strftime('%Y-%m-%d %H:%M')}] "
    for i, turn in enumerate(session):
        role = "User" if turn["role"] == "user" else "Assistant"
        transcript = f"{role}: {prefix}{turn['content']}"
        await svc.add_memory(
            transcript=transcript,
            client_id="experiment",
            source_id=f"exp_per_turn_{i}",
            user_id=user_id,
            user_email="exp@example.com",
            allow_update=False,
        )
    return group_id


async def _run_variant_user_only(
    svc: GraphitiMemoryService,
    user_id: str,
    session: list[dict[str, str]],
    ts: datetime,
) -> str:
    """Variant C: one episode per user turn only (drop assistant turns)."""
    _, group_id = await svc._get_graphiti(user_id)
    prefix = f"[{ts.strftime('%Y-%m-%d %H:%M')}] "
    n = 0
    for turn in session:
        if turn["role"] != "user":
            continue
        transcript = f"User: {prefix}{turn['content']}"
        await svc.add_memory(
            transcript=transcript,
            client_id="experiment",
            source_id=f"exp_user_only_{n}",
            user_id=user_id,
            user_email="exp@example.com",
            allow_update=False,
        )
        n += 1
    return group_id


VARIANTS = {
    "A_full_message": _run_variant_full_message,
    "B_per_turn": _run_variant_per_turn,
    "C_user_only": _run_variant_user_only,
}


async def main(argv: list[str]) -> int:
    selected = argv[1:] or list(VARIANTS.keys())
    for name in selected:
        if name not in VARIANTS:
            raise SystemExit(f"Unknown variant: {name}; choose from {list(VARIANTS)}")

    session, ts = _load_answer_session()
    print(
        f"Loaded answer session for {QID}: {len(session)} turns, "
        f"{sum(len(t['content']) for t in session)} chars, ts={ts.isoformat()}",
        flush=True,
    )

    svc = await _make_service()
    try:
        for name in selected:
            user_id = f"exp_{QID}_{name}"
            group_id = svc._graphiti_group_id(user_id)
            await _drop_graph(group_id)
            # graphiti caches a client per group_id, so wipe that too
            svc._clients.pop(group_id, None)

            print(
                f"\n--- Running variant {name} (graph={group_id}) ---", flush=True
            )
            t0 = datetime.now(timezone.utc)
            await VARIANTS[name](svc, user_id, session, ts)
            elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
            facts = await _list_facts(group_id)
            print(f"    elapsed: {elapsed:.1f}s", flush=True)
            _print_facts(name, facts)
    finally:
        svc.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv)))
