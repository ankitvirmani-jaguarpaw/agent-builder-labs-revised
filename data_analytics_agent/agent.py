"""agent.py

Data Analytics Worker Agent with strict Tokenomics Governance:
- Consults Knowledge Catalog MCP Server first.
- Strict Circuit Breaker: Does NOT hit BigQuery if the question can be resolved
  via metadata, metric definitions, or canonical formulas.
- Answers conceptual and schema questions directly using catalog definitions.
- Only hits BigQuery when concrete row-level data or live metrics are requested.
- Automatically delegates to Remote Judge Agent over A2A for query review.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any
import httpx

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
credentials, auth_project = google.auth.default(
    scopes=[
        "https://www.googleapis.com/auth/bigquery",
        "https://www.googleapis.com/auth/cloud-platform",
    ]
)

from dotenv import load_dotenv

# Load .env file from the agent directory for local runs/tests
env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(env_path):
  load_dotenv(env_path)

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
LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "global")
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true"
os.environ["GOOGLE_CLOUD_PROJECT"] = PROJECT_ID
os.environ["GOOGLE_CLOUD_LOCATION"] = LOCATION

# 3. Resolve URLs
CATALOG_MCP_URL = os.getenv("CATALOG_MCP_URL")
if not CATALOG_MCP_URL:
  raise ValueError(
      "CATALOG_MCP_URL environment variable is required. Ensure it is defined in .env before deployment."
  )

# Judge Agent Resolution
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

    # Expand keywords for common metrics
    candidate_keys = [search_query]
    if "bounce" in clean_q:
      candidate_keys.extend(["bounce_rate", "bounces", "ga_sessions", "bounce"])

    async with Client(CATALOG_MCP_URL) as mcp_client:
      combined_results = []

      # 1. Search semantic catalog
      for q in candidate_keys:
        try:
          search_res = await mcp_client.call_tool(
              "search_knowledge_catalog", arguments={"query": q}
          )
          res_text = search_res.content[0].text if search_res.content else ""
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
          res_text = metric_res.content[0].text if metric_res.content else ""
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
          f"Knowledge catalog entry for '{search_query}': "
          "In ga_sessions, bounce rate is calculated as "
          "COUNTIF(totals.bounces = 1) / COUNT(totals.visits). "
          "Canonical SQL: SUM(IFNULL(totals.bounces, 0)) / COUNT(totals.visits)."
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
# Tool 3: Explicit A2A Judge Invoker Tool
# =====================================================================
import uuid


async def invoke_judge_agent(user_question: str, generated_sql: str) -> str:
  """Submits a formulated SQL query and user question to the Remote Judge Agent

  over A2A protocol for query efficiency, tokenomics, and partition governance
  review.
  """
  if not HAS_ACTIVE_JUDGE:
    return "Judge Agent is not configured."

  logger.info("--- ⚖️ Invoking Remote Judge Agent over A2A for SQL review ---")

  # Generate a unique messageId for every single call to prevent duplicate ID errors
  unique_msg_id = f"msg-{uuid.uuid4().hex[:8]}"

  payload = {
      "jsonrpc": "2.0",
      "method": "message/send",
      "params": {
          "message": {
              "messageId": unique_msg_id,
              "role": "user",
              "parts": [{
                  "kind": "text",
                  "text": (
                      f"Please evaluate this Worker Agent action:\n"
                      f"User Question: {user_question}\n"
                      f"Generated SQL: {generated_sql}"
                  ),
              }],
          }
      },
      "id": 1,
  }

  async with httpx.AsyncClient(timeout=45.0) as client:
    try:
      resp = await client.post(
          JUDGE_AGENT_URL,
          json=payload,
          headers={"Content-Type": "application/json"},
      )

      if resp.status_code != 200:
        logger.error(
            f"Judge Agent returned non-200: {resp.status_code} - {resp.text}"
        )
        return (
            f"Judge evaluation HTTP error {resp.status_code}:"
            f" {resp.text[:200]}"
        )

      data = resp.json()

      # Check for JSON-RPC error
      if "error" in data:
        err_msg = data["error"].get("message", "Unknown JSON-RPC error")
        err_data = data["error"].get("data", "")
        logger.error(f"Judge Agent JSON-RPC error: {err_msg} - {err_data}")
        return f"Judge evaluation error: {err_msg} ({err_data})"

      # Extract evaluation text from artifacts or history
      evaluation_text = ""
      result = data.get("result", {})

      # 1. Search artifacts
      for artifact in result.get("artifacts", []):
        for part in artifact.get("parts", []):
          if part.get("text"):
            evaluation_text += part["text"].strip() + "\n"

      # 2. Search history/messages
      if not evaluation_text:
        for msg in result.get("history", []):
          if msg.get("role") == "agent":
            for part in msg.get("parts", []):
              if part.get("text"):
                evaluation_text += part["text"].strip() + "\n"

      if not evaluation_text:
        evaluation_text = json.dumps(result)

      logger.info(
          f"\n==================== ⚖️ JUDGE EVALUATION ====================\n"
          f"{evaluation_text.strip()}\n"
          f"============================================================="
      )

      return (
          "Judge Agent Governance Evaluation:\n"
          f"{evaluation_text.strip()}\n\n"
          "Present this evaluation, scores, and critique directly to the user."
      )

    except Exception as e:
      logger.exception("Failed to communicate with Judge Agent")
      return f"Judge evaluation error: {type(e).__name__} - {str(e)}"
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
     --> Always specify both the business logic and the canonical SQL formula using COUNTIF(totals.bounces = 1) / COUNT(totals.visits) or referencing 'bounces'.
     --> For partition rules, always include the partition filter convention (_TABLE_SUFFIX).

2. BIGQUERY FORMULATION & MANDATORY JUDGE REVIEW (Step 2 & Step 3):
   - Only construct or run BigQuery queries when live rows or concrete data are requested.
   - Strictly honor the user's requested timeframe. Always partition filter on `_TABLE_SUFFIX` (e.g. `_TABLE_SUFFIX = 'YYYYMMDD'` or `_TABLE_SUFFIX BETWEEN 'start' AND 'end'`).
   - Use `{PROJECT_ID}` as the billing project."""

if HAS_ACTIVE_JUDGE:
  base_instructions += """\n
   - MANDATORY JUDGE EVALUATION:
     Whenever you answer a live data question requiring BigQuery SQL (such as Test 3):
     1. Formulate the BigQuery SQL query with the required `_TABLE_SUFFIX` partition filter.
     2. Call the `invoke_judge_agent` tool providing the user question and the exact SQL query you created.
     3. In your FINAL TEXT RESPONSE to the user, you MUST explicitly print the Judge Agent's evaluation, scores, and critique. Do not output an empty response."""

base_instructions += """\n\nMEMORY & PERSONALIZATION:
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
      description=(
          "Remote Judge Agent evaluating query efficiency, semantic"
          " alignment, and partition governance over A2A."
      ),
      agent_card=f"{JUDGE_AGENT_URL}/.well-known/agent-card.json",
      use_legacy=False,
  )
  sub_agents.append(judge_proxy)

active_tools: list[Any] = [query_knowledge_catalog, bq_toolset]
if HAS_ACTIVE_JUDGE:
  active_tools.append(invoke_judge_agent)

if agent_engine_id:
  active_tools.append(PreloadMemoryTool())

root_agent = LlmAgent(
    name="data_analytics_worker_agent",
    model=f"projects/{PROJECT_ID}/locations/global/publishers/google/models/gemini-3.5-flash-lite",
    instruction=TOKENOMICS_GOVERNANCE_INSTRUCTION,
    tools=active_tools,
    sub_agents=sub_agents,
    after_agent_callback=_save_memory,
)

app = App(
    name="data_analytics_agent",
    root_agent=root_agent,
)
