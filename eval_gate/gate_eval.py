import argparse
import asyncio
import os
import sys
import subprocess
import pandas as pd
from dotenv import load_dotenv

# Resolve agent directory
PARENT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
AGENT_DIR = os.path.join(PARENT_DIR, "data_analytics_agent")
sys.path.insert(0, AGENT_DIR)

load_dotenv(os.path.join(AGENT_DIR, ".env"))

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("DEVSHELL_PROJECT_ID")
REGION = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")

import vertexai
from vertexai.evaluation import EvalTask, MetricPromptTemplateExamples

vertexai.init(project=PROJECT_ID, location=REGION)

from agent import app
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

TEST_CASES = [
    {
        "id": "case_1_conceptual_tokenomics",
        "prompt": "What is our business formula for bounce rate?",
        "reference": "The bounce rate formula is COUNTIF(totals.bounces = 1) / COUNT(totals.visits) or SUM(IFNULL(totals.bounces, 0)) / COUNT(totals.visits) as defined in the Knowledge Catalog.",
        "allowed_tools": ["query_knowledge_catalog", "adk_request_credential", "get_table_info"],
        "forbidden_tools": ["execute_sql"]
    },
    {
        "id": "case_2_schema_tokenomics",
        "prompt": "What are the partition rules for ga_sessions?",
        "reference": "The ga_sessions table is partitioned daily using the _TABLE_SUFFIX pseudo-column formatted as YYYYMMDD.",
        "allowed_tools": ["query_knowledge_catalog", "adk_request_credential", "get_table_info"],
        "forbidden_tools": ["execute_sql"]
    },
    {
        "id": "case_3_live_query_governance",
        "prompt": "Find the top 3 traffic sources in ga_sessions for August 1, 2017.",
        "reference": "Top 3 traffic sources on August 1, 2017 filtered with _TABLE_SUFFIX = '20170801' and evaluated by the Judge Agent.",
        "allowed_tools": ["query_knowledge_catalog", "invoke_judge_agent", "adk_request_credential", "get_table_info", "execute_sql"],
        "forbidden_tools": []
    },
    {
        "id": "case_4_prevent_full_table_scan",
        "prompt": "Give me all data from ga_sessions without any date filters.",
        "reference": "Queries on ga_sessions require a partition filter like _TABLE_SUFFIX to avoid costly full table scans. The Judge Agent rejects unpartitioned scans.",
        # Either consulting catalog or sending to judge to reject is valid governance
        "allowed_tools": ["query_knowledge_catalog", "invoke_judge_agent", "adk_request_credential", "get_table_info"],
        "forbidden_tools": ["execute_sql"]
    },
    {
        "id": "case_5_metrics_definition_lookup",
        "prompt": "How do we define total transactions in our ga_sessions dataset?",
        "reference": "Total transactions in ga_sessions are tracked via totals.transactions in the catalog metadata.",
        "allowed_tools": ["query_knowledge_catalog", "adk_request_credential", "get_table_info"],
        "forbidden_tools": ["execute_sql"]
    }
]


async def collect_agent_responses():
    print("⏳ Executing test cases against Agent to collect evaluation dataset...")
    session_service = InMemorySessionService()
    runner = Runner(app=app, session_service=session_service)

    records = []
    for case in TEST_CASES:
        session = await session_service.create_session(app_name=app.name, user_id="eval_user")
        user_content = types.Content(role="user", parts=[types.Part.from_text(text=case["prompt"])])

        response_text = ""
        tools_called = set()

        async for event in runner.run_async(user_id="eval_user", session_id=session.id, new_message=user_content):
            if hasattr(event, "content") and event.content and event.content.parts:
                for part in event.content.parts:
                    if hasattr(part, "text") and part.text:
                        response_text += part.text
                    if hasattr(part, "function_call") and part.function_call:
                        tools_called.add(part.function_call.name)

        # Governance: strictly verify no forbidden tools (like execute_sql) were executed
        forbidden_violation = any(tool in tools_called for tool in case["forbidden_tools"])
        governance_passed = not forbidden_violation

        records.append({
            "prompt": case["prompt"],
            "response": response_text or "No response generated.",
            "reference": case["reference"],
            "context": case["reference"],  # Groundedness evaluates response against context
            "tools_called": list(tools_called),
            "governance_passed": governance_passed
        })
        print(f"  • Processed '{case['id']}' | Tools: {list(tools_called)} | Governance Passed: {governance_passed}")

    return records


async def run_evaluation_gate(scenario: str):
    is_failing_scenario = (scenario == "fail")
    # Setting threshold: 0.65 for standard pass, 0.99 for fail scenario demonstration
    target_threshold = 0.99 if is_failing_scenario else 0.65

    print("===================================================================")
    print(f" 🧪 VERTEX AI GEN AI EVALUATION SERVICE: [{scenario.upper()} SCENARIO]")
    print(f" Target Quality Gate Threshold : {target_threshold * 100:.1f}%")
    print(f" Project                       : {PROJECT_ID}")
    print(f" Region                        : {REGION}")
    print("===================================================================\n")

    eval_records = await collect_agent_responses()
    eval_df = pd.DataFrame(eval_records)

    print("\n🤖 Calling Vertex AI Evaluation Service (EvalTask with LLM Judge)...")
    eval_task = EvalTask(
        dataset=eval_df[["prompt", "response", "reference", "context"]],
        metrics=[
            MetricPromptTemplateExamples.Pointwise.QUESTION_ANSWERING_QUALITY,
            MetricPromptTemplateExamples.Pointwise.GROUNDEDNESS
        ]
    )

    eval_result = eval_task.evaluate()

    summary = eval_result.summary_metrics
    qa_quality = summary.get("question_answering_quality/mean", 0.0)
    groundedness = summary.get("groundedness/mean", 0.0)

    # Convert 1-5 scale to 0.0-1.0
    norm_qa = (qa_quality / 5.0) if qa_quality > 1.0 else qa_quality
    norm_groundedness = (groundedness / 5.0) if groundedness > 1.0 else groundedness

    governance_mean = sum(1.0 for r in eval_records if r["governance_passed"]) / len(eval_records)
    composite_score = (norm_qa * 0.40) + (norm_groundedness * 0.30) + (governance_mean * 0.30)

    print("\n==================== 📊 EVALUATION SUMMARY ====================")
    print(f" • Vertex AI QA Quality Score  : {norm_qa * 100:.1f}% (Raw: {qa_quality:.2f})")
    print(f" • Vertex AI Groundedness Score: {norm_groundedness * 100:.1f}% (Raw: {groundedness:.2f})")
    print(f" • Tokenomics Governance Rate  : {governance_mean * 100:.1f}%")
    print(f" • Overall Composite Score     : {composite_score * 100:.1f}% (Required: {target_threshold * 100:.1f}%)")
    print("===============================================================\n")

    if composite_score < target_threshold:
        print(f"🛑 QUALITY GATE FAILED: Composite score ({composite_score * 100:.1f}%) < Threshold ({target_threshold * 100:.1f}%).")
        print("❌ Deployment aborted. No code has been deployed to Agent Engine.")
        sys.exit(1)

    print("🎉 ALL QUALITY GATES PASSED! Triggering Agent Engine deployment...\n")
    deploy_to_agent_engine()


def deploy_to_agent_engine():
    deploy_cmd = [
        "adk", "deploy", "agent_engine",
        f"--project={PROJECT_ID}",
        f"--region={REGION}",
        "--display_name=data_analytics_nogateway_nopsc_eval",
        f"--env_file={os.path.join(AGENT_DIR, '.env')}",
        "--otel_to_cloud",
        AGENT_DIR
    ]
    print(f"🚀 Running: {' '.join(deploy_cmd)}\n")
    proc = subprocess.run(deploy_cmd)
    if proc.returncode != 0:
        print("🛑 Deployment command failed.")
        sys.exit(proc.returncode)
    print("\n✅ Successfully deployed agent to Vertex AI Agent Engine!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Vertex AI Gen AI Evaluation and gate deployment.")
    parser.add_argument(
        "--scenario",
        choices=["pass", "fail"],
        default="pass",
        help="Run 'pass' to evaluate and deploy, or 'fail' to test quality gating abort."
    )
    args = parser.parse_args()
    asyncio.run(run_evaluation_gate(args.scenario))
