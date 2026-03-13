#!/usr/bin/env python3
"""
Verification script for mock STT and LLM servers.

Tests:
1. Mock LLM - models endpoint
2. Mock LLM - fact extraction
3. Mock LLM - memory updates
4. Mock LLM - embeddings
5. Mock Streaming STT - WebSocket connection and final results
"""

import asyncio
import json
import sys
import urllib.request

import websockets


def test_llm_models():
    """Test mock LLM models endpoint."""
    print("Testing Mock LLM - Models endpoint...")
    try:
        with urllib.request.urlopen("http://localhost:11435/v1/models") as response:
            data = json.loads(response.read())
            assert "data" in data
            assert len(data["data"]) == 2
            models = [m["id"] for m in data["data"]]
            assert "gpt-4o-mini" in models
            assert "text-embedding-3-small" in models
            print("  ✅ Models endpoint works")
            return True
    except Exception as e:
        print(f"  ❌ Models endpoint failed: {e}")
        return False


def test_llm_fact_extraction():
    """Test mock LLM fact extraction."""
    print("Testing Mock LLM - Fact extraction...")
    try:
        request_data = {
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": "FACT_RETRIEVAL_PROMPT extract facts"},
                {"role": "user", "content": "I like hiking"},
            ],
        }

        req = urllib.request.Request(
            "http://localhost:11435/v1/chat/completions",
            data=json.dumps(request_data).encode(),
            headers={"Content-Type": "application/json"},
        )

        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read())
            content = data["choices"][0]["message"]["content"]
            facts = json.loads(content)
            assert "facts" in facts
            assert len(facts["facts"]) > 0
            print(f"  ✅ Fact extraction works ({len(facts['facts'])} facts)")
            return True
    except Exception as e:
        print(f"  ❌ Fact extraction failed: {e}")
        return False


def test_llm_memory_update():
    """Test mock LLM memory updates."""
    print("Testing Mock LLM - Memory updates...")
    try:
        request_data = {
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": "UPDATE_MEMORY_PROMPT memory manager"},
                {"role": "user", "content": "User now likes apple pie"},
            ],
        }

        req = urllib.request.Request(
            "http://localhost:11435/v1/chat/completions",
            data=json.dumps(request_data).encode(),
            headers={"Content-Type": "application/json"},
        )

        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read())
            content = data["choices"][0]["message"]["content"]
            assert "<result>" in content
            assert "<memory>" in content
            assert "<item" in content
            print("  ✅ Memory updates work (XML response)")
            return True
    except Exception as e:
        print(f"  ❌ Memory updates failed: {e}")
        return False


def test_llm_embeddings():
    """Test mock LLM embeddings."""
    print("Testing Mock LLM - Embeddings...")
    try:
        request_data = {
            "model": "text-embedding-3-small",
            "input": ["test text 1", "test text 2"],
        }

        req = urllib.request.Request(
            "http://localhost:11435/v1/embeddings",
            data=json.dumps(request_data).encode(),
            headers={"Content-Type": "application/json"},
        )

        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read())
            assert "data" in data
            assert len(data["data"]) == 2
            assert len(data["data"][0]["embedding"]) == 1536
            assert len(data["data"][1]["embedding"]) == 1536

            # Test deterministic property
            embedding1_a = data["data"][0]["embedding"]

            # Make another request with same text
            with urllib.request.urlopen(req) as response2:
                data2 = json.loads(response2.read())
                embedding1_b = data2["data"][0]["embedding"]
                assert embedding1_a == embedding1_b

            print("  ✅ Embeddings work (1536 dims, deterministic)")
            return True
    except Exception as e:
        print(f"  ❌ Embeddings failed: {e}")
        return False


async def test_streaming_stt():
    """Test mock streaming STT server."""
    print("Testing Mock Streaming STT - WebSocket...")
    try:
        async with websockets.connect("ws://localhost:9999") as ws:
            # Receive initial empty result
            initial_msg = await ws.recv()
            initial_data = json.loads(initial_msg)
            assert initial_data["is_final"] == False

            # Send CloseStream
            await ws.send(json.dumps({"type": "CloseStream"}))

            # Receive final result
            final_msg = await ws.recv()
            final_data = json.loads(final_msg)

            # Verify nested structure
            assert "channel" in final_data
            assert "alternatives" in final_data["channel"]
            assert len(final_data["channel"]["alternatives"]) > 0

            alternative = final_data["channel"]["alternatives"][0]
            assert "transcript" in alternative
            assert "words" in alternative
            assert final_data["is_final"] == True

            # Verify speech detection thresholds
            words = alternative["words"]
            assert len(words) > 5, f"Need >5 words, got {len(words)}"

            duration = words[-1]["end"]
            assert duration > 2.0, f"Need >2.0s duration, got {duration}s"

            print(f"  ✅ Streaming STT works ({len(words)} words, {duration:.2f}s)")
            return True
    except Exception as e:
        print(f"  ❌ Streaming STT failed: {e}")
        return False


def main():
    """Run all verification tests."""
    print("\n" + "=" * 60)
    print("Mock Servers Verification")
    print("=" * 60 + "\n")

    results = []

    # Test LLM server
    results.append(test_llm_models())
    results.append(test_llm_fact_extraction())
    results.append(test_llm_memory_update())
    results.append(test_llm_embeddings())

    # Test streaming STT server
    results.append(asyncio.run(test_streaming_stt()))

    # Summary
    print("\n" + "=" * 60)
    passed = sum(results)
    total = len(results)
    print(f"Results: {passed}/{total} tests passed")
    print("=" * 60 + "\n")

    if passed == total:
        print("✅ All mock servers are working correctly!")
        return 0
    else:
        print(f"❌ {total - passed} test(s) failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
