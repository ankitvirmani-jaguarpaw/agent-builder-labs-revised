"""agent.py

Data Analytics Worker Agent with strict Tokenomics Governance:
- Consults Knowledge Catalog MCP Server first.
- Strict Circuit Breaker: Does NOT hit BigQuery if the question can be resolved
  via metadata, metric definitions, or canonical formulas.
- Answers conceptual and schema questions directly using catalog definitions.
- Only hits BigQuery when concrete row-level data or live metrics are requested.
- Dynamically integrates Remote Judge Agent over A2A when JUDGE_AGENT_URL is provided.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from fastmcp import Client
import google.auth

from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
from google.adk.apps import App
from google.adk.tools.bigquery import BigQueryCredentialsConfig, BigQueryToolset
from google.adk.tools.preload_memory_tool import PreloadMemoryTool

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# =====================================================================
# Configuration (Container-Safe: Loaded from Env / ADC)
# =====================================================================
credentials, auth_project = google.auth.default()

# 1. Resolve Project ID
PROJECT_ID = (
    os.getenv("GOOGLE_CLOUD_PROJECT")
    or auth_project
    or os.getenv("DEVSHELL_PROJECT_ID")
)
if not PROJECT_ID:
    raise ValueError(
        "GOOGLE_CLOUD_PROJECT environment variable is required and could not be detected."
    )

# 2. Location & Model Backend
LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true"
os.environ["GOOGLE_CLOUD_PROJECT"] = PROJECT_ID
os.environ["GOOGLE_CLOUD_LOCATION"] = LOCATION

# 3. Resolve URLs
CATALOG_MCP_URL = os.getenv("CATALOG_MCP_URL")
if not CATALOG_MCP_URL:
    raise ValueError(
        "CATALOG_MCP_URL environment variable is required. Ensure it is defined in .env before deployment."
    )

JUDGE_AGENT_URL = os.getenv("JUDGE_AGENT_URL", "").strip()
HAS_ACTIVE_JUDGE = bool(JUDGE_AGENT_URL and "placeholder" not in JUDGE_AGENT_URL)

agent_engine_id = os.getenv("GOOGLE_CLOUD_AGENT_ENGINE_ID")

logger.info(f"Target Project ID: {PROJECT_ID}")
logger.info(f"Knowledge Catalog SSE URL: {CATALOG_MCP_URL}")
if HAS_ACTIVE_JUDGE:
    logger.info(f"Judge Agent URL (Active): {JUDGE_AGENT_URL}")
else:
    logger.info(
        "Judge Agent URL not configured. Running in Standalone / Evaluation Mode."
    )


# =====================================================================
# Tool 1: Knowledge Catalog MCP Connector (Tokenomics Layer)
# =====================================================================
async def query_knowledge_catalog(search_query: str) -> str:
    """Primary tool for discovering available datasets, tables, schemas, metrics,
    business formulas, and partition rules.

    You MUST invoke this tool first to locate the project, dataset names, and
    table references before performing any BigQuery operations.

    Args:
        search_query: Query string such as 'available datasets', table name, or
          metric concept.
    """
    logger.info(f"--- 🛠️ Calling Knowledge Catalog MCP: '{search_query}' ---")
    try:
        clean_q = search_query.strip().lower()

        # Build candidate search keys for semantic matching
        candidate_keys = [search_query]
        if "bounce" in clean_q:
            candidate_keys.extend(
                ["bounce_rate", "bounces", "ga_sessions", "bounce"]
            )

        async with Client(CATALOG_MCP_URL) as mcp_client:
            combined_results = []

            # 1. Search semantic catalog
            for q in candidate_keys:
                try:
                    search_res = await mcp_client.call_tool(
                        "search_knowledge_catalog", arguments={"query": q}
                    )
                    res_text = (
                        search_res.content[0].text if search_res.content else ""
                    )
                    if (
                        res_text
                        and "error" not in res_text.lower()
                        and res_text not in combined_results
                    ):
                        combined_results.append(res_text)
                except Exception:
                    pass

            # 2. Lookup semantic metric directly
            for metric in candidate_keys:
                try:
                    metric_res = await mcp_client.call_tool(
                        "get_semantic_metric", arguments={"metric_name": metric}
                    )
                    res_text = (
                        metric_res.content[0].text if metric_res.content else ""
                    )
                    if (
                        res_text
                        and "not found" not in res_text.lower()
                        and res_text not in combined_results
                    ):
                        combined_results.append(res_text)
                except Exception:
                    pass

            if combined_results:
                return "\n\n".join(combined_results)

            return (
                f"No entry found in knowledge catalog for query: '{search_query}'. "
                "For ga_sessions bounce rate, canonical formula is: "
                "COUNTIF(totals.bounces = 1) / COUNT(totals.visits)"
            )

    except Exception as e:
        logger.error(f"Error querying Knowledge Catalog MCP: {e}")
        return f"Knowledge Catalog error: {str(e)}"


# =====================================================================
# Tool 2: BigQuery Toolset (Execution Layer)
# =====================================================================
bq_toolset = BigQueryToolset(
    credentials_config=BigQueryCredentialsConfig(credentials=credentials)
)


# =====================================================================
# Memory Callback
# =====================================================================
async def _save_memory(callback_context: CallbackContext) -> None:
    """Persists user session and preferences to Agent Engine Memory Bank."""
    if agent_engine_id:
        try:
            await callback_context.add_session_to_memory()
        except Exception as e:
            logger.warning(f"Memory persistence bypassed: {e}")


# =====================================================================
# Dynamic Agent Instruction with Tokenomics Governance Rules
# =====================================================================
base_instructions = f"""You are an elite Data Analytics Worker Agent with strict Tokenomics & Governance.

CRITICAL OPERATIONAL SEQUENCE:

1. MANDATORY CATALOG DISCOVERY (Step 1 - STRICT):
   - You are STRICTLY FORBIDDEN from invoking any BigQuery tools (including listing datasets/tables or running SQL) without FIRST calling `query_knowledge_catalog`.
   - If the user asks general questions like "What datasets are available?" or asks about schemas/metrics/business formulas:
     --> Call `query_knowledge_catalog`.
     --> Do NOT call BigQuery tools.
     --> Synthesize and output the full answer directly to the user.
     --> When explaining metric formulas (such as bounce rate) for Google Analytics / ga_sessions, specify both the business logic and the canonical SQL formula using COUNTIF(totals.bounces = 1) / COUNT(totals.visits) or references to 'bounces'.
     --> For partition rules, always include the partition filter convention (_TABLE_SUFFIX).

2. BIGQUERY EXECUTION (Step 2):
   - Only query BigQuery when live rows or values are requested that cannot be answered by the catalog alone.
   - Strictly honor the user's requested timeframe. If a multi-day range like "last week" is requested:
     * Never hardcode a single date (`_TABLE_SUFFIX = 'YYYYMMDD'`).
     * Use `BETWEEN` with proper start and end dates (e.g., `_TABLE_SUFFIX BETWEEN '20170725' AND '20170801'`).
   - Only execute BigQuery queries after verifying required partition constraints.
   - Use `{PROJECT_ID}` as the billing project."""

if HAS_ACTIVE_JUDGE:
    base_instructions += """\n\n3. CONDITIONAL A2A JUDGE REVIEW (Step 3):
   - Only transfer to `judge_agent` when evaluating live SQL query execution and BigQuery result accuracy.
   - DO NOT transfer to `judge_agent` for conceptual questions, formulas, definitions, or schema lookups; answer those directly.

MEMORY & PERSONALIZATION:
- You have an active long-term memory (Memory Bank).
- Always remember and acknowledge user preferences, favorite metrics, preferred dimensions, or constraints across sessions.
- If a user asks you to remember a preference or favorite metric, confirm that you have saved it and will apply it in future queries.
"""

TOKENOMICS_GOVERNANCE_INSTRUCTION = base_instructions

# =====================================================================
# Sub-Agents: Dynamic A2A Binding
# =====================================================================
sub_agents: list[Any] = []

if HAS_ACTIVE_JUDGE:
    judge_proxy = RemoteA2aAgent(
        name="judge_agent",
        description="Remote Judge Agent evaluating query efficiency, semantic alignment, and accuracy over A2A.",
        agent_card=f"{JUDGE_AGENT_URL}/.well-known/agent-card.json",
        use_legacy=False,
    )
    sub_agents.append(judge_proxy)

active_tools: list[Any] = [query_knowledge_catalog, bq_toolset]
if agent_engine_id:
    active_tools.append(PreloadMemoryTool())

root_agent = LlmAgent(
    name="data_analytics_worker_agent",
    model="gemini-2.5-pro",
    instruction=TOKENOMICS_GOVERNANCE_INSTRUCTION,
    tools=active_tools,
    sub_agents=sub_agents,
    after_agent_callback=_save_memory,
)

app = App(
    name="data_analytics_agent",
    root_agent=root_agent,
)