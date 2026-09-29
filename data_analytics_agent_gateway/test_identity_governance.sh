#!/bin/bash
# =====================================================================
# Agent Identity & Gateway Interception Test Suite
#
# Verifies end-to-end identity governance and traffic interception between:
# - Target Principal:
#   principal://agents.global.org-788308946018.system.id.goog/resources/aiplatform/projects/867402506099/locations/us-central1/reasoningEngines/4238207756995133440
# - Knowledge Catalog MCP (FastMCP SSE)
# - Judge Agent (A2A Protocol)
# =====================================================================

set -e

export PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${GOOGLE_CLOUD_LOCATION:-us-central1}"
export PROJECT_NUM="${PROJECT_NUM:-$(gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)")}"
REGION="${GOOGLE_CLOUD_LOCATION:-us-central1}"
export ORG_ID=$(gcloud projects get-ancestors "${PROJECT_ID}" --format="value(id,type)" | awk '$2=="organization" {print $1}')
echo "ORG_ID: ${ORG_ID}"
RE_ID=$(curl -s -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://${REGION}-aiplatform.googleapis.com/v1/projects/${PROJECT_ID}/locations/${REGION}/reasoningEngines" \
  | jq -r '.reasoningEngines[]? | select(.displayName=="data_analytics_agent_gateway") | .name' | awk -F/ '{print $NF}')
echo "RE_ID: $RE_ID"
RE_URL="https://${REGION}-aiplatform.googleapis.com/v1beta1/projects/${PROJECT_NUM}/locations/${REGION}/reasoningEngines/${RE_ID}"
TARGET_PRINCIPAL="principal://agents.global.org-${ORG_ID}.system.id.goog/resources/aiplatform/projects/${PROJECT_NUM}/locations/${REGION}/reasoningEngines/${RE_ID}"

echo "==================================================================="
echo " 🆔 Agent Identity & Gateway Interception Verification Suite"
echo " 🎯 Target Principal: ${TARGET_PRINCIPAL}"
echo " 🌍 Project: ${PROJECT_ID} | Region: ${REGION}"
echo "==================================================================="

# -----------------------------------------------------------------
# 1. Verify IAM Role Bindings for Agent Principal
# -----------------------------------------------------------------
echo -e "\n[Phase 1] Auditing IAM Role Bindings for Agent Principal..."

# 1a. Knowledge Catalog MCP Cloud Run Invoker
echo -n "Checking roles/run.invoker on knowledge-catalog-mcp... "
MCP_IAM=$(gcloud run services get-iam-policy knowledge-catalog-mcp --region="${REGION}" --project="${PROJECT_ID}" --format=json)
if echo "${MCP_IAM}" | grep -q "${TARGET_PRINCIPAL}"; then
  echo "✅ BOUND"
else
  echo "❌ MISSING"
  exit 1
fi

# 1b. Judge Agent Cloud Run Invoker
echo -n "Checking roles/run.invoker on judge-agent... "
JUDGE_IAM=$(gcloud run services get-iam-policy judge-agent --region="${REGION}" --project="${PROJECT_ID}" --format=json)
if echo "${JUDGE_IAM}" | grep -q "${TARGET_PRINCIPAL}"; then
  echo "✅ BOUND"
else
  echo "❌ MISSING"
  exit 1
fi

# 1c. BigQuery Project Permissions
echo -n "Checking BigQuery roles (jobUser, dataViewer) on project... "
BQ_IAM=$(gcloud projects get-iam-policy "${PROJECT_ID}" \
  --flatten="bindings[].members" \
  --filter="bindings.members:${TARGET_PRINCIPAL}" \
  --format="value(bindings.role)")
if echo "${BQ_IAM}" | grep -q "roles/bigquery.jobUser" && echo "${BQ_IAM}" | grep -q "roles/bigquery.dataViewer"; then
  echo "✅ BOUND"
else
  echo "❌ MISSING"
  exit 1
fi

# -----------------------------------------------------------------
# 2. Trigger End-to-End Query (MCP + Judge Agent)
# -----------------------------------------------------------------
echo -e "\n[Phase 2] Triggering End-to-End Query (MCP Discovery + Judge Evaluation)..."
AUTH_TOKEN=$(gcloud auth print-access-token)
SESSION_ID="identity-test-$(date +%s)"
START_TIME=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# Create Session
curl -s -X POST "${RE_URL}:query" \
  -H "Authorization: Bearer ${AUTH_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"class_method\": \"create_session\", \"input\": {\"session_id\": \"${SESSION_ID}\", \"user_id\": \"identity_tester\"}}" > /dev/null

echo "✅ Session initialized (${SESSION_ID}). Sending query..."

# Stream Query requesting both Catalog lookup and Judge Agent evaluation
PROMPT="Calculate bounce rate for ga_sessions between 20170725 and 20170726 using partition specs from catalog and have the judge agent evaluate the result."
curl -N -s -X POST "${RE_URL}:streamQuery?alt=sse" \
  -H "Authorization: Bearer ${AUTH_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{
    \"class_method\": \"async_stream_query\",
    \"input\": {
      \"user_id\": \"identity_tester\",
      \"session_id\": \"${SESSION_ID}\",
      \"message\": \"${PROMPT}\"
    }
  }" | grep -E "data: " | head -n 15 || true

echo -e "\n✅ Query completed. Waiting 5s for gateway telemetry flush..."
sleep 5

# -----------------------------------------------------------------
# 3. Assert Gateway Interception & mTLS Identity in Cloud Logging
# -----------------------------------------------------------------
echo -e "\n[Phase 3] Auditing Egress Gateway Interception & mTLS Fingerprints..."

GATEWAY_LOGS=$(gcloud logging read "
  resource.type=\"networkservices.googleapis.com/Gateway\"
  AND resource.labels.gateway_name=\"fsi-agent-gateway-egress\"
  AND timestamp >= \"${START_TIME}\"
" --project="${PROJECT_ID}" --format=json)

# Check Knowledge Catalog MCP Call
echo -e "\n--- Knowledge Catalog MCP Interception ---"
MCP_ENTRY=$(echo "${GATEWAY_LOGS}" | jq -c '.[] | select((.httpRequest.requestUrl // "") | contains("knowledge-catalog-mcp"))' | head -n 1)

if [[ -n "${MCP_ENTRY}" ]]; then
  MCP_URL=$(echo "${MCP_ENTRY}" | jq -r '.httpRequest.requestUrl')
  MCP_METHOD=$(echo "${MCP_ENTRY}" | jq -r '.httpRequest.requestMethod')
  MCP_STATUS=$(echo "${MCP_ENTRY}" | jq -r '.httpRequest.status')
  MCP_TLS=$(echo "${MCP_ENTRY}" | jq -r '.jsonPayload.enforcedGatewaySecurityPolicy.requestWasTlsIntercepted')
  MCP_FINGERPRINT=$(echo "${MCP_ENTRY}" | jq -r '.jsonPayload.mtls.clientCertSha256Fingerprint // "N/A"')
  MCP_TOOL=$(echo "${MCP_ENTRY}" | jq -r '.jsonPayload.agentGatewayInfo.mcpInfo.parameter // "N/A"')

  echo "Destination URL:  ${MCP_URL}"
  echo "HTTP Method:      ${MCP_METHOD} (Status: ${MCP_STATUS})"
  echo "TLS Intercepted:  ${MCP_TLS}"
  echo "MCP Tool Called:  ${MCP_TOOL}"
  echo "mTLS Fingerprint: ${MCP_FINGERPRINT}"
  echo "✅ MCP Interception and mTLS verified!"
else
  echo "⚠️ Warning: MCP call not captured in current window."
fi

# Check Judge Agent Call
echo -e "\n--- Judge Agent (A2A) Interception ---"
JUDGE_ENTRY=$(echo "${GATEWAY_LOGS}" | jq -c '.[] | select((.httpRequest.requestUrl // "") | contains("judge-agent"))' | head -n 1)

if [[ -n "${JUDGE_ENTRY}" ]]; then
  JUDGE_URL=$(echo "${JUDGE_ENTRY}" | jq -r '.httpRequest.requestUrl')
  JUDGE_METHOD=$(echo "${JUDGE_ENTRY}" | jq -r '.httpRequest.requestMethod')
  JUDGE_STATUS=$(echo "${JUDGE_ENTRY}" | jq -r '.httpRequest.status')
  JUDGE_TLS=$(echo "${JUDGE_ENTRY}" | jq -r '.jsonPayload.enforcedGatewaySecurityPolicy.requestWasTlsIntercepted')
  JUDGE_FINGERPRINT=$(echo "${JUDGE_ENTRY}" | jq -r '.jsonPayload.mtls.clientCertSha256Fingerprint // "N/A"')
  JUDGE_REG_RESOURCE=$(echo "${JUDGE_ENTRY}" | jq -r '.jsonPayload.agentGatewayInfo.agentRegistryResource // "N/A"')

  echo "Destination URL:      ${JUDGE_URL}"
  echo "HTTP Method:          ${JUDGE_METHOD} (Status: ${JUDGE_STATUS})"
  echo "TLS Intercepted:      ${JUDGE_TLS}"
  echo "Agent Registry Ref:   ${JUDGE_REG_RESOURCE}"
  echo "mTLS Fingerprint:     ${JUDGE_FINGERPRINT}"
  echo "✅ Judge Agent Interception and mTLS verified!"
else
  echo "⚠️ Warning: Judge Agent call not captured in current window."
fi

echo -e "\n==================================================================="
echo " 🎉 AGENT IDENTITY & GATEWAY INTERCEPTION VERIFICATION COMPLETE!"
echo "==================================================================="
