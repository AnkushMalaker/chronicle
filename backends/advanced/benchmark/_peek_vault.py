"""Print every ConvDoc the new prompt produced for one regressed user."""
import asyncio
import sys
sys.path.insert(0, "/app")
from advanced_omi_backend.services.memory import get_memory_service

QID = "66f24dbb"  # "yellow dress" regression


async def main():
    svc = get_memory_service()
    if not svc._initialized:
        await svc.initialize()
    user_id = f"bench-{QID}"
    vault = svc.vault
    conv_ids = vault.list_docs(user_id)
    print(f"# {len(conv_ids)} docs for {user_id}\n")
    for cid in conv_ids:
        body = vault.read_doc(user_id, cid)
        if "yellow" in body.lower() or "dress" in body.lower() or "sister" in body.lower():
            print("=" * 70)
            print(f"DOC {cid}:")
            print(body[:2500])


if __name__ == "__main__":
    asyncio.run(main())
