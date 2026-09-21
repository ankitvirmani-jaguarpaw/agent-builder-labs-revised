"""test_judge_local.py

Runs the Judge Agent locally with an in-memory ADK runner against:
- A bad query (SELECT * scan, should FAIL)
- A compliant query (partition filter, catalog alignment, should PASS)
"""

from __future__ import annotations

import asyncio
import json
from google.adk.apps import App
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

# Import the judge agent from your agent.py
from agent import judge_agent

TEST_CASES = [
    {
        "name": "Tokenomics Violation (SELECT * full scan)",
        "prompt": (
            "Worker Analytical Submission:\n"
            "- Question: What was the bounce rate for last week?\n"
            "- SQL Executed: SELECT * FROM `bigquery-public-data.google_analytics_sample.ga_sessions_*`\n"
            "- Calculation: Calculated total visits divided by total bounces.\n"
            "- Summary Claim: Bounce rate was 42.5% across 10,000 sessions."
        ),
        "expected_pass": False,
    },
    {
        "name": "Compliant Execution (Partition filter & specific projection)",
        "prompt": (
            "Worker Analytical Submission:\n"
            "- Question: What were the total transactions in US for 2024-01-01 to 2024-01-07?\n"
            "- SQL Executed: SELECT SUM(totals.transactions) AS tx_count FROM `project.dataset.ga_sessions_*` "
            "WHERE _TABLE_SUFFIX BETWEEN '20240101' AND '20240107' AND geoNetwork.country = 'United States'\n"
            "- Calculation: Summed transactions field as per official catalog metric 'transaction_volume'.\n"
            "- Summary Claim: The total transactions for US was 1,230."
        ),
        "expected_pass": True,
    },
]


async def run_test():
    # 1. Wrap the agent in an App and Runner
    app = App(name="judge_app", root_agent=judge_agent)
    session_service = InMemorySessionService()
    runner = Runner(app=app, session_service=session_service)

    print("==================================================")
    print(" Running Local Judge Agent Tests")
    print("==================================================\n")

    for case in TEST_CASES:
        print(f"--- Running: {case['name']} ---")

        session = await session_service.create_session(
            app_name=app.name,
            user_id="test_runner",
        )

        user_content = types.Content(
            role="user",
            parts=[types.Part.from_text(text=case["prompt"])],
        )

        response_text = ""
        async for event in runner.run_async(
            user_id="test_runner",
            session_id=session.id,
            new_message=user_content,
        ):
            if hasattr(event, "content") and event.content and event.content.parts:
                for part in event.content.parts:
                    if hasattr(part, "text") and part.text:
                        response_text += part.text

        # 2. Verify Output is Valid JSON
        print("\nRaw LLM Output:")
        print(response_text)

        try:
            # Strip markdown formatting backticks if present
            cleaned_json = response_text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            parsed = json.loads(cleaned_json)

            scores = parsed.get("scores", {})
            passed = parsed.get("passed")

            print(f"Parsed Scores: {scores}")
            print(f"Verdict: Passed={passed} (Expected={case['expected_pass']})")

            # Check required fields
            assert "query_efficiency" in scores, "Missing query_efficiency score"
            assert "semantic_alignment" in scores, "Missing semantic_alignment score"
            assert "accuracy" in scores, "Missing accuracy score"
            assert "preference_adherence" in scores, "Missing preference_adherence score"
            print(" Result: ✅ Valid JSON and schema compliant\n")

        except Exception as e:
            print(f" Result: ❌ Validation error: {e}\n")


if __name__ == "__main__":
    asyncio.run(run_test())