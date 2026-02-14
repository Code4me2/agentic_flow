#!/usr/bin/env python3
"""Test async delegation with push notifications and turn-boundary injection."""

import asyncio
import os
import sys

# Reduce log noise but keep important messages
os.environ.setdefault("DEBUG", "")

from manager_agent import ManagerAgent, USE_PUSH_NOTIFICATIONS, HAS_FASTAPI

async def test_async_flow(use_push: bool = True):
    """Test async delegation with optional push notifications."""
    mode = "PUSH NOTIFICATIONS" if use_push else "POLLING"
    print(f"=== Async Turn-Boundary Injection Test ({mode}) ===\n")

    if use_push and not HAS_FASTAPI:
        print("FastAPI not available, falling back to polling")
        use_push = False

    manager = ManagerAgent(use_push=use_push)
    await manager.setup(["http://localhost:8002"])

    print(f"Discovered {len(manager.registry.list_agents())} agent(s)")
    print(f"Push notifications: {'enabled' if manager._use_push else 'disabled (polling)'}\n")

    # Give webhook server time to start
    if manager._use_push:
        await asyncio.sleep(1)

    # Test 1: Async delegation
    print("[1] Sending async task: 'Write hello world in Python'")
    response = await manager.process(
        "Write a simple hello world function in Python. Use async mode."
    )
    print(f"    Manager: {response[:100]}...")
    print(f"    Pending tasks: {manager.get_pending_task_count()}\n")

    # Test 2: Continue conversation while task runs
    print("[2] Asking question while task runs: 'What is 5 * 7?'")
    response = await manager.process("What is 5 * 7?")
    print(f"    Manager: {response}\n")

    # Test 3: Wait for turn-boundary injection
    if manager._use_push:
        print("[3] Waiting for push notification...")
    else:
        print("[3] Polling for async result...")

    for i in range(25):
        await asyncio.sleep(2)

        # Turn boundary check
        announcement = await manager.check_and_inject_results()
        if announcement:
            print(f"\n{'='*60}")
            print("ASYNC RESULT INJECTED AT TURN BOUNDARY")
            print('='*60)
            print(announcement[:500])
            if len(announcement) > 500:
                print("... (truncated)")
            print('='*60 + "\n")
            break

        pending = manager.get_pending_task_count()
        mode_str = "push" if manager._use_push else "poll"
        print(f"    Waiting ({mode_str})... ({(i+1)*2}s, {pending} pending)")
    else:
        print("    Timed out waiting for result")

    await manager.shutdown()
    print("Test complete.")


async def compare_modes():
    """Compare push notifications vs polling."""
    print("=== Comparing Push Notifications vs Polling ===\n")

    # Test with polling first
    print("--- Testing with POLLING ---")
    await test_async_flow(use_push=False)

    print("\n" + "="*60 + "\n")

    # Test with push notifications
    print("--- Testing with PUSH NOTIFICATIONS ---")
    await test_async_flow(use_push=True)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--poll":
        asyncio.run(test_async_flow(use_push=False))
    elif len(sys.argv) > 1 and sys.argv[1] == "--compare":
        asyncio.run(compare_modes())
    else:
        # Default: use push notifications
        asyncio.run(test_async_flow(use_push=True))
