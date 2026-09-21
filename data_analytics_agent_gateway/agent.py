"""data_analytics_agent_gateway/agent.py

Data Analytics Worker Agent integrated with Google Cloud Agent Gateway (Ingress & Egress):
- Intercepted by Agent Gateway Ingress for Model Armor prompt injection & DLP redaction.
- Intercepted by Agent Gateway Egress for Model Armor governance & IAP verification.
- Consults Knowledge Catalog MCP Server first (Tokenomics Governance).
- Strict Circuit Breaker: Does NOT hit BigQuery if questions can be resolved via metadata.
- Only hits BigQuery when concrete row-level data or live metrics are requested.
- Dynamically integrates Remote Judge Agent over A2A when JUDGE_AGENT_URL is provided.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any
import google.auth
from dotenv import load_dotenv
from fastmcp import Client

load_dotenv()

# Normalize SSL CA cert bundle path between container and local host environments
_ca_cert = os.getenv("SSL_CERT_FILE")
if _ca_cert and not os.path.exists(_ca_cert):
    _local_ca = os.path.join(os.path.dirname(__file__), "gateway-ca.crt")
    if os.path.exists(_local_ca):
        os.environ["SSL_CERT_FILE"] = _local_ca
        os.environ["REQUESTS_CA_BUNDLE"] = _local_ca
    else:
        os.environ.pop("SSL_CERT_FILE", None)
        os.environ.pop("REQUESTS_CA_BUNDLE", None)

from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.apps import App
from google.adk.tools.bigquery import BigQueryCredentialsConfig, BigQueryToolset
from google.adk.tools.preload_memory_tool import PreloadMemoryTool
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent

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
MODEL_NAME = os.getenv("MODEL_NAME", "gemini-2.5-flash-lite")
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true"
os.environ["GOOGLE_CLOUD_PROJECT"] = PROJECT_ID
os.environ["GOOGLE_CLOUD_LOCATION"] = LOCATION

# 3. Resolve Service URLs
CATALOG_MCP_URL = os.getenv("CATALOG_MCP_URL")
if not CATALOG_MCP_URL:
    raise ValueError(
        "CATALOG_MCP_URL environment variable is required. Ensure it is defined in .env before deployment."
    )

JUDGE_AGENT_URL = os.getenv("JUDGE_AGENT_URL", "").strip()
HAS_ACTIVE_JUDGE = bool(JUDGE_AGENT_URL and "placeholder" not in JUDGE_AGENT_URL)

agent_engine_id = os.getenv("GOOGLE_CLOUD_AGENT_ENGINE_ID")

# ---------------------------------------------------------------------
# Full Telemetry & Observability Configuration (Cloud Trace, Metrics, Logging)
# ---------------------------------------------------------------------
os.environ.setdefault("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
os.environ.setdefault("ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS", "true")
os.environ.setdefault("ADK_EXPERIMENTAL_TELEMETRY", "true")
os.environ.setdefault("ADK_CAPTURE_MCP_HTTP_BODIES", "true")

try:
    from google.adk.telemetry.google_cloud import get_gcp_exporters, get_gcp_resource
    from google.adk.telemetry.setup import maybe_set_otel_providers

    otel_hooks = get_gcp_exporters(
        enable_cloud_tracing=True,
        enable_cloud_metrics=True,
        enable_cloud_logging=True,
        google_auth=(credentials, PROJECT_ID),
    )
    otel_resource = get_gcp_resource(project_id=PROJECT_ID)
    maybe_set_otel_providers([otel_hooks], otel_resource=otel_resource)
    logger.info("✅ Full Telemetry initialized: Cloud Trace, Cloud Monitoring, Cloud Logging")
except Exception as e:
    logger.warning("Telemetry auto-instrumentation warning: %s", e)

logger.info(f"Target Project ID: {PROJECT_ID}")
logger.info(f"Target Location:   {LOCATION}")
logger.info(f"Default Model:     {MODEL_NAME}")
logger.info(f"Knowledge Catalog SSE URL: {CATALOG_MCP_URL}")
if HAS_ACTIVE_JUDGE:
    logger.info(f"Judge Agent URL (Active): {JUDGE_AGENT_URL}")
else:
    logger.info("Judge Agent URL not configured. Running in Standalone Mode.")



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
        async with Client(CATALOG_MCP_URL) as mcp_client:
            # 1. Search semantic catalog
            search_res = await mcp_client.call_tool(
                "search_knowledge_catalog",
                arguments={"query": search_query},
            )
            raw_text = search_res.content[0].text if search_res.content else ""

            if "results" in raw_text:
                return raw_text

            # 2. Fallback to direct metric lookup
            metric_res = await mcp_client.call_tool(
                "get_semantic_metric",
                arguments={"metric_name": search_query},
            )
            return metric_res.content[0].text if metric_res.content else raw_text

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
base_instructions = f"""You are an elite Data Analytics Worker Agent operating under strict Tokenomics & Gateway Governance.

CRITICAL OPERATIONAL SEQUENCE:

1. MANDATORY CATALOG DISCOVERY (Step 1 - STRICT):
   - You are STRICTLY FORBIDDEN from invoking any BigQuery tools (including listing datasets/tables or running SQL) without FIRST calling `query_knowledge_catalog`.
   - If the user asks general questions like "What datasets are available?" or asks about schemas/metrics:
     --> Call `query_knowledge_catalog(search_query="available datasets")`.
     --> Do NOT call BigQuery `list_datasets`.

2. BIGQUERY EXECUTION (Step 2):
   - Only query BigQuery when live rows or values are requested that cannot be answered by the catalog.
   - Always use the exact fully-qualified table/dataset path discovered in the Knowledge Catalog (e.g., `bigquery-public-data.google_analytics_sample.ga_sessions_*`). Do NOT replace the catalog's project ID with your billing project ID.
   - Strictly honor the user's requested timeframe. If a multi-day range like "last week" is requested:
     * Never hardcode a single date (`_TABLE_SUFFIX = 'YYYYMMDD'`).
     * Use `BETWEEN` with proper start and end dates (e.g., `_TABLE_SUFFIX BETWEEN '20170725' AND '20170801'`).
   - Only execute BigQuery queries after verifying required partition constraints.
   - Use `{PROJECT_ID}` as the billing project."""

if HAS_ACTIVE_JUDGE:
    base_instructions += """

3. MANDATORY A2A JUDGE REVIEW (Step 3):
   - Transfer to `judge_agent` before returning analytical answers to the user."""

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

root_agent = LlmAgent(
    name="data_analytics_worker_agent",
    model=MODEL_NAME,
    instruction=TOKENOMICS_GOVERNANCE_INSTRUCTION,
    tools=[
        query_knowledge_catalog,
        bq_toolset,
        PreloadMemoryTool(),
    ],
    sub_agents=sub_agents,
    after_agent_callback=_save_memory,
)

app = App(
    name="data_analytics_agent_gateway",
    root_agent=root_agent,
)
