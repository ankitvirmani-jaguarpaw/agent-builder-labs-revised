#!/usr/bin/env bash
set -eo pipefail

export PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
export REGION="${REGION:-$(gcloud config get-value compute/region 2>/dev/null)}"
[[ -z "${REGION}" ]] && export REGION="us-central1"

echo "==================================================================="
echo " Synchronizing Agent Registry (A2MCP & A2A Services)"
echo " Project: ${PROJECT_ID} | Region: ${REGION}"
echo "==================================================================="

# 1. Discover live Cloud Run URLs
MCP_RUN_URL=$(gcloud run services describe knowledge-catalog-mcp --region="${REGION}" --project="${PROJECT_ID}" --format="value(status.url)" 2>/dev/null || true)
JUDGE_RUN_URL=$(gcloud run services describe judge-agent --region="${REGION}" --project="${PROJECT_ID}" --format="value(status.url)" 2>/dev/null || true)

if [[ -z "${MCP_RUN_URL}" ]]; then
  echo "⚠️ Warning: knowledge-catalog-mcp Cloud Run service not found in ${REGION}."
else
  CATALOG_MCP_URL="${MCP_RUN_URL}/sse"
  echo "Knowledge Catalog SSE URL: ${CATALOG_MCP_URL}"
  echo "Updating knowledge-catalog-mcp in Agent Registry..."
  gcloud agent-registry services update knowledge-catalog-mcp \
    --location="${REGION}" \
    --project="${PROJECT_ID}" \
    --interfaces="protocolBinding=JSONRPC,url=${CATALOG_MCP_URL}" --quiet
  echo "✅ knowledge-catalog-mcp synchronized in Agent Registry."
fi

if [[ -z "${JUDGE_RUN_URL}" ]]; then
  echo "⚠️ Warning: judge-agent Cloud Run service not found in ${REGION}."
else
  echo "Judge Agent URL: ${JUDGE_RUN_URL}"
  # Ensure A2A_PUBLIC_URL matches in Cloud Run env
  CURRENT_A2A_ENV=$(gcloud run services describe judge-agent --region="${REGION}" --project="${PROJECT_ID}" --format="value(spec.template.spec.containers[0].env)" 2>/dev/null || true)
  if [[ "${CURRENT_A2A_ENV}" != *"${JUDGE_RUN_URL}"* ]]; then
    echo "Updating A2A_PUBLIC_URL env var on judge-agent..."
    gcloud run services update judge-agent \
      --region="${REGION}" \
      --project="${PROJECT_ID}" \
      --update-env-vars="A2A_PUBLIC_URL=${JUDGE_RUN_URL}" --quiet
  fi

  # Fetch live agent card and update registry
  CARD_CONTENT=$(curl -s "${JUDGE_RUN_URL}/.well-known/agent-card.json")
  if [[ -n "${CARD_CONTENT}" && "${CARD_CONTENT}" != *"404"* ]]; then
    echo "Updating judge-agent in Agent Registry..."
    gcloud agent-registry services update judge-agent \
      --location="${REGION}" \
      --project="${PROJECT_ID}" \
      --agent-spec-type=A2A_AGENT_CARD \
      --agent-spec-content="${CARD_CONTENT}" --quiet
    echo "✅ judge-agent synchronized in Agent Registry."
  fi
fi

# 3. Synchronize Internal Platform & Data Endpoints
echo "Synchronizing Internal Service Endpoints (BigQuery, Telemetry, Vertex AI)..."
register_or_update_endpoint() {
  local svc_name="$1"
  local display_name="$2"
  local url="$3"

  if gcloud agent-registry services describe "${svc_name}" --location="${REGION}" --project="${PROJECT_ID}" > /dev/null 2>&1; then
    gcloud agent-registry services update "${svc_name}" \
      --location="${REGION}" \
      --project="${PROJECT_ID}" \
      --interfaces="protocolBinding=JSONRPC,url=${url}" --quiet
  else
    gcloud agent-registry services create "${svc_name}" \
      --location="${REGION}" \
      --project="${PROJECT_ID}" \
      --display-name="${display_name}" \
      --endpoint-spec-type=no-spec \
      --interfaces="protocolBinding=JSONRPC,url=${url}" --quiet
  fi
  echo "✅ Endpoint ${svc_name} synchronized (${url})."
}

register_or_update_endpoint "bigquery-api" "BigQuery API" "https://bigquery.mtls.googleapis.com"
register_or_update_endpoint "telemetry-api" "Telemetry API" "https://telemetry.mtls.googleapis.com"
register_or_update_endpoint "vertex-aiplatform-api" "Vertex AI Platform API" "https://us-central1-aiplatform.mtls.googleapis.com"

echo "==================================================================="
echo " 🎉 Agent Registry Sync Complete!"
echo "==================================================================="

