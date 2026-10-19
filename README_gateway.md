# Deploy the Data Analytics Agent behind Google Cloud Agent Gateway (Egress Governance)

This runbook guides you through provisioning an **Egress Agent Gateway** (`AGENT_TO_ANYWHERE`) with **Agent Registry** service allowlisting, configuring **IAP v2 Authorization**, and deploying the **Data Analytics Worker Agent** (powered by **Gemini 3.8 Flash**) to **Vertex AI Agent Engine**.

---

## Architecture Overview

```text
                               AGENT GATEWAY (EGRESS)
  ┌──────────────────────┐    ┌─────────────────────────┐    ┌───────────────────────────────┐
  │  Data Analytics      │    │ 1. Agent Registry       │    │ Cloud Run: Judge Agent (A2A)  │
  │  Worker Agent        ├───►│    (Allowlist / Deny-by-├───►├───────────────────────────────┤
  │  (Agent Engine)      │    │     Default)            │    │ Cloud Run: Catalog MCP (SSE)  │
  │                      │    │                         │    ├───────────────────────────────┤
  │  identity_type:      │    │ 2. Egress IAP v2        │    │ BigQuery API & Storage        │
  │  AGENT_IDENTITY      │    │    (DRY_RUN Mode)       │    ├───────────────────────────────┤
  └──────────────────────┘    └─────────────────────────┘    │ Vertex AI & Token Services    │
                                                             └───────────────────────────────┘
```

1. **Deny-by-Default (Agent Registry)**: The gateway drops all outbound traffic unless the destination URL is explicitly registered.
2. **Composite Google APIs Endpoint (`core-gapi-services`)**: Groups all Google APIs (`aiplatform`, `bigquery`, `bigquerystorage`, `cloudresourcemanager`, `iamcredentials`, `sts`, `oauth2`, `logging`) into a single registry entry.
3. **Dedicated Tool Endpoints**: The **Judge Agent** (Cloud Run A2A) and **Knowledge Catalog MCP** (Cloud Run SSE) are registered as distinct services.
4. **IAP v2 Authorization Extension**: Configured in `DRY_RUN` mode (`failOpen: true`) for initial validation. (IAM Unified Access Policies for strict `ENFORCE` mode can be applied later).

---

## Step 0 — Set Environment Variables

Run from your Cloud Shell terminal:

```bash
export PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
export REGION="us-central1"
export PROJECT_NUM=$(gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)")
export GATEWAY_NAME="fsi-agent-gateway-egress"
export AUTH_TOKEN=$(gcloud auth print-access-token)

echo "Project ID:  ${PROJECT_ID} (${PROJECT_NUM})"
echo "Region:      ${REGION}"
echo "Gateway:     ${GATEWAY_NAME}"
```

---

## Step 1 — Enable Required Google Cloud APIs

```bash
gcloud services enable \
  networkservices.googleapis.com \
  agentregistry.googleapis.com \
  serviceextensions.googleapis.com \
  networksecurity.googleapis.com \
  iap.googleapis.com \
  aiplatform.googleapis.com \
  bigquery.googleapis.com \
  dataplex.googleapis.com \
  --project="${PROJECT_ID}"
```

---

## Step 2 — Register Services in Agent Registry

The egress gateway enforces a strict **deny-by-default** model. All destinations the agent touches must be registered.

```bash
# Discover live Cloud Run dependency URLs
export CATALOG_URL=$(gcloud run services describe knowledge-catalog-mcp \
  --region="${REGION}" --project="${PROJECT_ID}" --format='value(status.url)')
export JUDGE_URL=$(gcloud run services describe judge-agent \
  --region="${REGION}" --project="${PROJECT_ID}" --format='value(status.url)')

echo "Catalog MCP URL: ${CATALOG_URL}"
echo "Judge Agent URL: ${JUDGE_URL}"

# 1. Register Composite Core Google APIs
gcloud agent-registry services delete core-gapi-services \
  --location="${REGION}" --project="${PROJECT_ID}" --quiet 2>/dev/null || true

gcloud agent-registry services create core-gapi-services \
  --project="${PROJECT_ID}" \
  --location="${REGION}" \
  --display-name="Core Google APIs" \
  --description="Essential Google APIs for BigQuery, Vertex AI, and Token Services" \
  --endpoint-spec-type=no-spec \
  --interfaces=protocolBinding=JSONRPC,url=https://aiplatform.googleapis.com \
  --interfaces=protocolBinding=JSONRPC,url=https://bigquery.googleapis.com \
  --interfaces=protocolBinding=JSONRPC,url=https://bigquerystorage.googleapis.com \
  --interfaces=protocolBinding=JSONRPC,url=https://cloudresourcemanager.googleapis.com \
  --interfaces=protocolBinding=JSONRPC,url=https://iamcredentials.googleapis.com \
  --interfaces=protocolBinding=JSONRPC,url=https://sts.googleapis.com \
  --interfaces=protocolBinding=JSONRPC,url=https://oauth2.googleapis.com \
  --interfaces=protocolBinding=JSONRPC,url=https://logging.googleapis.com \
  --interfaces=protocolBinding=JSONRPC,url=https://telemetry.googleapis.com \
  --interfaces=protocolBinding=JSONRPC,url=https://agentregistry.googleapis.com

# 2. Register Judge Agent (A2A)
gcloud agent-registry services delete judge-agent-service \
  --location="${REGION}" --project="${PROJECT_ID}" --quiet 2>/dev/null || true

gcloud agent-registry services create judge-agent-service \
  --project="${PROJECT_ID}" \
  --location="${REGION}" \
  --display-name="Judge Agent Service" \
  --endpoint-spec-type=no-spec \
  --interfaces=protocolBinding=JSONRPC,url="${JUDGE_URL}"

# 3. Register Knowledge Catalog MCP (Base & SSE)
gcloud agent-registry services delete catalog-mcp-service \
  --location="${REGION}" --project="${PROJECT_ID}" --quiet 2>/dev/null || true

gcloud agent-registry services create catalog-mcp-service \
  --project="${PROJECT_ID}" \
  --location="${REGION}" \
  --display-name="Knowledge Catalog MCP Service" \
  --endpoint-spec-type=no-spec \
  --interfaces=protocolBinding=JSONRPC,url="${CATALOG_URL}" \
  --interfaces=protocolBinding=JSONRPC,url="${CATALOG_URL}/sse"

echo "✅ All egress destinations registered in Agent Registry."
```

---

## Step 3 — Deploy the Egress Agent Gateway & IAP Authz Policy

```bash
mkdir -p cfg

# 1. Create the Gateway Manifest
cat << EOF > cfg/fsi-agent-gateway-egress.yaml
googleManaged:
  governedAccessPath: AGENT_TO_ANYWHERE
protocols:
  - MCP
  - PROTOCOL_UNSPECIFIED
registries:
  - "//agentregistry.googleapis.com/projects/${PROJECT_ID}/locations/${REGION}"
EOF

# 2. Import the Agent Gateway
gcloud beta network-services agent-gateways import "${GATEWAY_NAME}" \
  --location="${REGION}" \
  --project="${PROJECT_ID}" \
  --source=cfg/fsi-agent-gateway-egress.yaml

# 3. Grant IAM roles to Gateway Callout Identity
GW_CARD=$(gcloud network-services agent-gateways describe "${GATEWAY_NAME}" \
  --location="${REGION}" --project="${PROJECT_ID}" --format="json(agentGatewayCard)")
GW_SA=$(echo "${GW_CARD}" | jq -r '.agentGatewayCard.serviceExtensionsServiceAccount // empty')

if [ -n "${GW_SA}" ]; then
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${GW_SA}" --role="roles/iap.httpsResourceAccessor" \
    --condition=None --quiet >/dev/null
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${GW_SA}" --role="roles/run.invoker" \
    --condition=None --quiet >/dev/null
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${GW_SA}" --role="roles/serviceusage.serviceUsageConsumer" \
    --condition=None --quiet >/dev/null
fi

# 4. Create IAP v2 Service Extension (DRY_RUN mode)
cat << 'EOF' > cfg/fsi-agent-gateway-egress-iap.yaml
name: fsi-agent-gateway-egress-iap
service: iap.googleapis.com
failOpen: true
timeout: 1s
metadata:
  iamEnforcementMode: "ENFORCE"
  iapPolicyVersion: "V2"
EOF

gcloud beta service-extensions authz-extensions import fsi-agent-gateway-egress-iap \
  --source="cfg/fsi-agent-gateway-egress-iap.yaml" \
  --location="${REGION}" --project="${PROJECT_ID}" --quiet

# 5. Attach the Authz Policy to the Gateway
AUTHZ_URL="https://networksecurity.googleapis.com/v1alpha1/projects/${PROJECT_ID}/locations/${REGION}/authzPolicies"
POLICY_ID="fsi-agent-gateway-egress-iap-policy"

curl -s -X POST \
  -H "Authorization: Bearer ${AUTH_TOKEN}" \
  -H "Content-Type: application/json" \
  -H "x-goog-user-project: ${PROJECT_ID}" \
  "${AUTHZ_URL}?authz_policy_id=${POLICY_ID}" \
  -d "{
    \"action\": \"CUSTOM\",
    \"target\": {
      \"resources\": [\"projects/${PROJECT_ID}/locations/${REGION}/agentGateways/${GATEWAY_NAME}\"]
    },
    \"customProvider\": {
      \"authzExtension\": {
        \"resources\": [\"projects/${PROJECT_ID}/locations/${REGION}/authzExtensions/fsi-agent-gateway-egress-iap\"]
      }
    }
  }" >/dev/null || true

echo "✅ Gateway and IAP policy deployed successfully."
```

---

## Step 4 — Grant Worker Agent IAM Permissions

The worker agent runs under the default Compute Engine service account inside Agent Engine:

```bash
WORKER_SA="${PROJECT_NUM}-compute@developer.gserviceaccount.com"

# Cloud Run Invoker on Judge and Catalog
gcloud run services add-iam-policy-binding judge-agent \
  --region="${REGION}" --project="${PROJECT_ID}" \
  --member="serviceAccount:${WORKER_SA}" \
  --role="roles/run.invoker" --quiet >/dev/null

gcloud run services add-iam-policy-binding knowledge-catalog-mcp \
  --region="${REGION}" --project="${PROJECT_ID}" \
  --member="serviceAccount:${WORKER_SA}" \
  --role="roles/run.invoker" --quiet >/dev/null

# BigQuery Job User & Data Viewer
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${WORKER_SA}" \
  --role="roles/bigquery.jobUser" --condition=None --quiet >/dev/null

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${WORKER_SA}" \
  --role="roles/bigquery.dataViewer" --condition=None --quiet >/dev/null
```

---

## Step 5 — Configure `data_analytics_agent_gateway`

Prepare the agent directory for deployment:

```bash
mkdir -p data_analytics_agent_gateway

# 1. Write .agent_engine_config.json
cat << EOF > data_analytics_agent_gateway/.agent_engine_config.json
{
  "identity_type": "AGENT_IDENTITY",
  "agent_gateway_config": {
    "agent_to_anywhere_config": {
      "agent_gateway": "projects/${PROJECT_ID}/locations/${REGION}/agentGateways/${GATEWAY_NAME}"
    }
  }
}
EOF

# 2. Write .env
cat << EOF > data_analytics_agent_gateway/.env
GOOGLE_CLOUD_PROJECT=${PROJECT_ID}
GOOGLE_CLOUD_LOCATION=global
MODEL_NAME=gemini-3.8-flash
CATALOG_MCP_URL=${CATALOG_URL}/sse
JUDGE_AGENT_URL=${JUDGE_URL}
ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=true
OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true
EOF

# 3. Write requirements.txt
cat << 'EOF' > data_analytics_agent_gateway/requirements.txt
google-adk[a2a,mcp,gcp,otel-gcp]>=2.6.0
google-cloud-dataplex
google-cloud-bigquery
google-genai>=1.27.0
google-auth>=2.0.0
python-dotenv>=1.1.0
fastmcp
EOF

# 4. Write __init__.py
cat << 'EOF' > data_analytics_agent_gateway/__init__.py
from .agent import app, root_agent

__all__ = ["app", "root_agent"]
EOF

# 5. Write agent.py
cat << 'EOF' > data_analytics_agent_gateway/agent.py
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

# Accept local Envoy egress proxy interception certificates
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

# Clean ambient BigQueryToolset initialization
bq_toolset = BigQueryToolset()

async def _save_memory(callback_context: CallbackContext) -> None:
  if agent_engine_id:
    try:
      await callback_context.add_session_to_memory()
    except Exception as e:
      logger.warning(f"Memory persistence skipped: {e}")

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
EOF
```

---

## Step 6 — Deploy to Vertex AI Agent Engine

From the base repository directory:

```bash
cd ~/agent-builder-labs-revised

adk deploy agent_engine \
  --project="$(gcloud config get-value project)" \
  --region="us-central1" \
  --display_name="data_analytics_gateway_governed" \
  --otel_to_cloud \
  data_analytics_agent_gateway
```

---

## Step 7 — Verification & Live Querying

### Automated Query Script

Create and execute `query_deployed_agent.sh` to test session creation, the Knowledge Catalog circuit-breaker, and live BigQuery execution:

```bash
cat << 'EOF' > query_deployed_agent.sh
#!/usr/bin/env bash
set -eo pipefail

export PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
export REGION="us-central1"
export DISPLAY_NAME="data_analytics_gateway_governed"
export PROJECT_NUM=$(gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)")
export TOKEN=$(gcloud auth print-access-token)

# 1. Resolve Reasoning Engine ID
ENGINES_JSON=$(curl -s \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "x-goog-user-project: ${PROJECT_ID}" \
  "https://${REGION}-aiplatform.googleapis.com/v1/projects/${PROJECT_NUM}/locations/${REGION}/reasoningEngines")

REASONING_ENGINE_ID=$(echo "${ENGINES_JSON}" | jq -r --arg NAME "${DISPLAY_NAME}" '
  .reasoningEngines // []
  | map(select(.displayName == $NAME))
  | sort_by(.createTime) | reverse
  | .[0].name | split("/")[-1] // empty
')

if [ -z "${REASONING_ENGINE_ID}" ]; then
  REASONING_ENGINE_ID=$(echo "${ENGINES_JSON}" | jq -r '
    .reasoningEngines // []
    | sort_by(.createTime) | reverse
    | .[0].name | split("/")[-1] // empty
  ')
fi

export BASE_ENDPOINT="https://${REGION}-aiplatform.googleapis.com/v1/projects/${PROJECT_NUM}/locations/${REGION}/reasoningEngines/${REASONING_ENGINE_ID}"

echo "✅ Active Reasoning Engine ID: ${REASONING_ENGINE_ID}"
echo "   Endpoint:                   ${BASE_ENDPOINT}"

# 2. Create stateful session
echo -e "\n--- Step 1: Creating Session for user 'analyst_1' ---"
SESSION_RESP=$(curl -s -X POST \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -H "x-goog-user-project: ${PROJECT_ID}" \
  "${BASE_ENDPOINT}:query" \
  -d '{
    "class_method": "async_create_session",
    "input": {
      "user_id": "analyst_1"
    }
  }')

SESSION_ID=$(echo "$SESSION_RESP" | jq -r '.output.id // empty')
echo "✅ Session Established: ${SESSION_ID}"

# 3. Test 1: Conceptual Metadata Query (Knowledge Catalog only)
echo -e "\n==================================================================="
echo " Test 1: Conceptual Metadata Query (Tokenomics Circuit Breaker)"
echo " Prompt: 'What is our business formula for bounce rate?'"
echo "==================================================================="

curl -N -s -X POST \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -H "x-goog-user-project: ${PROJECT_ID}" \
  "${BASE_ENDPOINT}:streamQuery?alt=sse" \
  -d "{
    \"class_method\": \"async_stream_query\",
    \"input\": {
      \"user_id\": \"analyst_1\",
      \"session_id\": \"${SESSION_ID}\",
      \"message\": \"What is our business formula for bounce rate?\"
    }
  }"

# 4. Test 2: Live BigQuery Query + A2A Judge Evaluation
echo -e "\n\n==================================================================="
echo " Test 2: Live BigQuery Query + A2A Judge Evaluation"
echo " Prompt: 'Find the top 3 traffic sources in ga_sessions for August 1, 2017.'"
echo "==================================================================="

curl -N -s -X POST \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -H "x-goog-user-project: ${PROJECT_ID}" \
  "${BASE_ENDPOINT}:streamQuery?alt=sse" \
  -d "{
    \"class_method\": \"async_stream_query\",
    \"input\": {
      \"user_id\": \"analyst_1\",
      \"session_id\": \"${SESSION_ID}\",
      \"message\": \"Find the top 3 traffic sources in ga_sessions for August 1, 2017.\"
    }
  }"
echo ""
EOF

chmod +x query_deployed_agent.sh
./query_deployed_agent.sh
```
