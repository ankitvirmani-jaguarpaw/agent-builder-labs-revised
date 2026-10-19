"""agent.py
Data Analytics Worker Agent Governed by Google Cloud Agent Gateway.
Powered by Gemini 3.8 Flash under Tokenomics Governance.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
from typing import Any
import httpx

# Disable SSL verification for internal Envoy egress interception proxy
_orig_ssl_context = ssl.create_default_context
def _unverified_context(*args, **kwargs):
    ctx = _orig_ssl_context(*args, **kwargs)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx
ssl.create_default_context = _unverified_context

from fastmcp import Client
import google.auth
from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
from google.adk.apps import App
from google.adk.tools.bigquery import BigQueryToolset
from google.adk.tools.preload_memory_tool import PreloadMemoryTool
from dotenv import load_dotenv

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(env_path):
  load_dotenv(env_path)

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT")
if not PROJECT_ID:
  _, auth_project = google.auth.default()
  PROJECT_ID = auth_project

LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "global")
MODEL_NAME = os.getenv("MODEL_NAME", "gemini-3.8-flash")
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true"
os.environ["GOOGLE_CLOUD_PROJECT"] = PROJECT_ID
os.environ["GOOGLE_CLOUD_LOCATION"] = LOCATION

CATALOG_MCP_URL = os.getenv("CATALOG_MCP_URL")
JUDGE_AGENT_URL = os.getenv("JUDGE_AGENT_URL", "").strip()
HAS_ACTIVE_JUDGE = bool(JUDGE_AGENT_URL and "placeholder" not in JUDGE_AGENT_URL)
agent_engine_id = os.getenv("GOOGLE_CLOUD_AGENT_ENGINE_ID")

async def query_knowledge_catalog(search_query: str) -> str:
  """Discovers available datasets, schemas, metrics, and business formulas."""
  logger.info(f"--- Calling Knowledge Catalog MCP: '{search_query}' ---")
  try:
    async with Client(CATALOG_MCP_URL) as mcp_client:
      search_res = await mcp_client.call_tool(
          "search_knowledge_catalog", arguments={"query": search_query}
      )
      raw_text = search_res.content[0].text if search_res.content else ""
      if "results" in raw_text:
        return raw_text
      metric_res = await mcp_client.call_tool(
          "get_semantic_metric", arguments={"metric_name": search_query}
      )
      return metric_res.content[0].text if metric_res.content else raw_text
  except Exception as e:
    logger.error(f"Error querying Knowledge Catalog MCP: {e}")
    return f"Knowledge Catalog error: {str(e)}"

# Ambient project credentials
bq_toolset = BigQueryToolset()

async def _save_memory(callback_context: CallbackContext) -> None:
  if agent_engine_id:
    try:
      await callback_context.add_session_to_memory()
    except Exception as e:
      logger.warning(f"Memory persistence bypassed: {e}")

base_instructions = f"""You are an elite Data Analytics Worker Agent operating under strict Tokenomics & Gateway Governance.

CRITICAL OPERATIONAL SEQUENCE:
1. MANDATORY CATALOG DISCOVERY:
   - Always call query_knowledge_catalog first for metrics, schemas, and dataset discovery.
   - Do NOT run BigQuery queries for conceptual or schema questions.
2. BIGQUERY EXECUTION:
   - Only query BigQuery when concrete row-level data is required.
   - Always partition filter on _TABLE_SUFFIX using BETWEEN date ranges.
   - Use {PROJECT_ID} as billing project."""

if HAS_ACTIVE_JUDGE:
  base_instructions += "\n3. MANDATORY A2A JUDGE REVIEW: Transfer to judge_agent for validation before delivering results."

TOKENOMICS_GOVERNANCE_INSTRUCTION = base_instructions

sub_agents: list[Any] = []
if HAS_ACTIVE_JUDGE:
  judge_proxy = RemoteA2aAgent(
      name="judge_agent",
      description="Remote Judge Agent evaluating query efficiency and governance over A2A.",
      agent_card=f"{JUDGE_AGENT_URL}/.well-known/agent-card.json",
      use_legacy=False,
  )
  sub_agents.append(judge_proxy)

root_agent = LlmAgent(
    name="data_analytics_worker_agent",
    model=MODEL_NAME,
    instruction=TOKENOMICS_GOVERNANCE_INSTRUCTION,
    tools=[query_knowledge_catalog, bq_toolset, PreloadMemoryTool()],
    sub_agents=sub_agents,
    after_agent_callback=_save_memory,
)

app = App(
    name="data_analytics_agent_gateway",
    root_agent=root_agent,
)
