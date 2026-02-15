#!/usr/bin/env python3
"""
Test script for the Orchestrator.

Tests:
1. Health check
2. Model listing
3. Simple chat completion
4. Agent delegation (if agents available)

Usage:
    python test_orchestrator.py [--with-agents]
"""

import argparse
import asyncio
import json
import sys

import httpx


ORCH_URL = "http://localhost:8000"


async def test_health():
    """Test health endpoint."""
    print("\n=== Testing Health ===")

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{ORCH_URL}/health")
            resp.raise_for_status()
            data = resp.json()
            print(f"Status: {data.get('status')}")
            print(f"Agents: {data.get('agents', [])}")
            print(f"Model: {data.get('model')}")
            return True
        except Exception as e:
            print(f"Health check failed: {e}")
            return False


async def test_models():
    """Test models endpoint."""
    print("\n=== Testing Models ===")

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{ORCH_URL}/v1/models")
            resp.raise_for_status()
            data = resp.json()
            models = data.get("data", [])
            print(f"Found {len(models)} models:")
            for m in models[:5]:
                print(f"  - {m.get('id')}")
            return True
        except Exception as e:
            print(f"Models test failed: {e}")
            return False


async def test_simple_chat():
    """Test simple chat completion."""
    print("\n=== Testing Simple Chat ===")

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            resp = await client.post(
                f"{ORCH_URL}/v1/chat/completions",
                json={
                    "model": "ministral-3:14b",
                    "messages": [
                        {"role": "user", "content": "Say 'Hello from orchestrator' and nothing else."}
                    ],
                    "stream": False,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            print(f"Response: {content[:200]}")
            return True
        except Exception as e:
            print(f"Chat test failed: {e}")
            return False


async def test_streaming_chat():
    """Test streaming chat completion."""
    print("\n=== Testing Streaming Chat ===")

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            async with client.stream(
                "POST",
                f"{ORCH_URL}/v1/chat/completions",
                json={
                    "model": "ministral-3:14b",
                    "messages": [
                        {"role": "user", "content": "Count from 1 to 5."}
                    ],
                    "stream": True,
                },
            ) as resp:
                resp.raise_for_status()

                content_parts = []
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                            delta = data.get("choices", [{}])[0].get("delta", {})
                            if "content" in delta:
                                content_parts.append(delta["content"])
                                print(delta["content"], end="", flush=True)
                        except:
                            pass

                print()
                print(f"Total chunks: {len(content_parts)}")
                return True
        except Exception as e:
            print(f"Streaming test failed: {e}")
            return False


async def test_agent_delegation():
    """Test agent delegation (requires agents running)."""
    print("\n=== Testing Agent Delegation ===")
    print("(This test requires A2A bridges running on :8001 and :8002)")

    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            # First check if agents are discovered
            resp = await client.get(f"{ORCH_URL}/health")
            agents = resp.json().get("agents", [])

            if not agents:
                print("No agents discovered. Start bridges first.")
                return False

            print(f"Agents available: {agents}")

            # Send a message that should trigger delegation
            async with client.stream(
                "POST",
                f"{ORCH_URL}/v1/chat/completions",
                json={
                    "model": "ministral-3:14b",
                    "messages": [
                        {
                            "role": "system",
                            "content": "You have access to agents via delegate_to_* tools. Use them when appropriate."
                        },
                        {
                            "role": "user",
                            "content": "Delegate to LocalCoder to write a hello world in Python."
                        }
                    ],
                    "stream": True,
                    "session_id": "test-agent-session",
                },
            ) as resp:
                resp.raise_for_status()

                print("Streaming response:")
                async for line in resp.aiter_lines():
                    if line.startswith("event: "):
                        event_type = line[7:]
                        print(f"\n[Event: {event_type}]", end="")
                    elif line.startswith("data: "):
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            print("\n[DONE]")
                            break
                        try:
                            data = json.loads(data_str)
                            # Check for custom events
                            if "task_id" in data:
                                print(f"\n[Agent pending: {data}]")
                            elif "count" in data:
                                print(f"\n[Injection: {data}]")
                            else:
                                delta = data.get("choices", [{}])[0].get("delta", {})
                                if "content" in delta:
                                    print(delta["content"], end="", flush=True)
                        except:
                            pass

                return True
        except Exception as e:
            print(f"Agent delegation test failed: {e}")
            return False


async def main():
    parser = argparse.ArgumentParser(description="Test the Orchestrator")
    parser.add_argument("--with-agents", action="store_true", help="Include agent delegation test")
    args = parser.parse_args()

    print("=" * 60)
    print("Orchestrator Test Suite")
    print("=" * 60)
    print(f"Target: {ORCH_URL}")

    results = {}

    # Basic tests
    results["health"] = await test_health()

    if not results["health"]:
        print("\nOrchestrator not running. Start with: ./run.sh orchestrator")
        sys.exit(1)

    results["models"] = await test_models()
    results["simple_chat"] = await test_simple_chat()
    results["streaming"] = await test_streaming_chat()

    if args.with_agents:
        results["agent_delegation"] = await test_agent_delegation()

    # Summary
    print("\n" + "=" * 60)
    print("Results:")
    print("=" * 60)
    for test, passed in results.items():
        status = "PASS" if passed else "FAIL"
        print(f"  {test}: {status}")

    all_passed = all(results.values())
    print("=" * 60)
    print(f"Overall: {'ALL PASSED' if all_passed else 'SOME FAILED'}")

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    asyncio.run(main())
