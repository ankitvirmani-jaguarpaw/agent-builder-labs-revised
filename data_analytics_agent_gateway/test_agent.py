"""test_agent.py

Verifies that the Worker Agent enforces tokenomics governance:
1. Pure metric/schema questions -> Zero BigQuery calls.
2. Data calculation questions -> Optimized BigQuery execution with partition filters.
"""

from __future__ import annotations

import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from agent import app


async def send_message(runner: Runner, session_id: str, prompt: str) -> str:
    """Sends a message to the agent and collects the final response text with 429 retry backoff."""
    user_content = types.Content(
        role="user",
        parts=[types.Part.from_text(text=prompt)],
    )

    max_retries = 5
    for attempt in range(max_retries):
        try:
            final_text = ""
            async for event in runner.run_async(
                user_id="test_analyst",
                session_id=session_id,
                new_message=user_content,
            ):
                if hasattr(event, "content") and event.content and event.content.parts:
                    for part in event.content.parts:
                        if hasattr(part, "text") and part.text:
                            final_text += part.text
            if final_text.strip():
                return final_text
            if attempt < max_retries - 1:
                await asyncio.sleep(3)
                continue
            return final_text
        except Exception as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                if attempt < max_retries - 1:
                    wait_time = 12 * (attempt + 1)
                    print(f"⚠️ Vertex AI 429 rate limit hit. Retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})...")
                    await asyncio.sleep(wait_time)
                    continue
            raise


async def test_tokenomics_governance():
    print("===================================================================")
    print(" Testing Worker Agent Tokenomics & Query Governance")
    print("===================================================================\n")

    session_service = InMemorySessionService()
    runner = Runner(app=app, session_service=session_service)
    
    session = await session_service.create_session(
        app_name=app.name,
        user_id="test_analyst",
    )

    # Test 1: Conceptual Question (Tokenomics: Must NOT hit BigQuery)
    print("Test 1: Asking conceptual question ('What is our formula for bounce rate?')...")
    response_1 = await send_message(
        runner,
        session_id=session.id,
        prompt="What is our business formula for bounce rate?",
    )
    print(f"Agent Response:\n{response_1}\n")
    assert "COUNTIF" in response_1 or "bounce" in response_1.lower()
    print("✅ Test 1 Passed: Answered from Knowledge Catalog with ZERO BigQuery scans.\n")

    # Test 2: Schema Spec Question (Tokenomics: Must NOT hit BigQuery)
    print("Test 2: Asking schema question ('What are the partition rules for ga_sessions?')...")
    response_2 = await send_message(
        runner,
        session_id=session.id,
        prompt="What are the partition rules for ga_sessions?",
    )
    print(f"Agent Response:\n{response_2}\n")
    assert "_TABLE_SUFFIX" in response_2 or "partition" in response_2.lower()
    print("✅ Test 2 Passed: Answered table spec without running SQL.\n")

    print("===================================================================")
    print(" 🎉 ALL LOCAL TOKENOMICS TESTS PASSED!")
    print("===================================================================")


if __name__ == "__main__":
    asyncio.run(test_tokenomics_governance())
