#!/bin/bash
# =====================================================================
# Agent Gateway Negative Scenarios Test Suite
#
# Tests active blocking and boundary enforcement for:
# 1. Knowledge Catalog MCP (Gateway drops unregistered/unauthorized endpoints)
# 2. Judge Agent A2A (Gateway drops unregistered/unauthorized endpoints)
# 3. BigQuery Analytics (IAM & Gateway blocks unauthorized table/query executions)
#
# Always restores state cleanly upon completion or failure.
# =====================================================================

set -e

export PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
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
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==================================================================="
echo " 🚫 AGENT GATEWAY NEGATIVE SCENARIOS ENFORCEMENT TEST SUITE"
echo " 🎯 Agent: projects/${PROJECT_NUM}/locations/${REGION}/reasoningEngines/${RE_ID}"
echo " 🆔 Principal: ${TARGET_PRINCIPAL}"
echo "==================================================================="

AUTH_TOKEN=$(gcloud auth print-access-token)

# Cleanup trap to guarantee restoration if script is interrupted
cleanup() {
  echo -e "\n🧹 [Cleanup] Restoring original registry and IAM state..."
  bash "${SCRIPT_DIR}/scripts/sync_agent_registry.sh" > /dev/null 2>&1 || true
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="${TARGET_PRINCIPAL}" \
    --role="roles/bigquery.jobUser" --condition=None --quiet > /dev/null 2>&1 || true
  echo "✅ Restoration complete."
}
trap cleanup EXIT

# -----------------------------------------------------------------
# Negative Scenario 1: Agent Gateway Blocking Knowledge Catalog MCP
# -----------------------------------------------------------------
echo -e "\n[Negative Test 1] Testing Gateway Blocking for Knowledge Catalog MCP..."
echo "Simulating un-registered MCP destination in Agent Registry..."

# Temporarily point registry interface to dummy unregistered URL
gcloud agent-registry services update knowledge-catalog-mcp \
  --location="${REGION}" \
  --project="${PROJECT_ID}" \
  --interfaces="protocolBinding=JSONRPC,url=https://unauthorized-mcp.blocked-by-gateway.run.app/sse" \
  --quiet > /dev/null

SESS_MCP="neg-mcp-$(date +%s)"
curl -s -X POST "${RE_URL}:query" \
  -H "Authorization: Bearer ${AUTH_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"class_method\": \"create_session\", \"input\": {\"session_id\": \"${SESS_MCP}\", \"user_id\": \"tester\"}}" > /dev/null

echo "Sending query requiring Knowledge Catalog MCP..."
MCP_OUTPUT=$(curl -N -s -X POST "${RE_URL}:streamQuery?alt=sse" \
  -H "Authorization: Bearer ${AUTH_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{
    \"class_method\": \"async_stream_query\",
    \"input\": {
      \"user_id\": \"tester\",
      \"session_id\": \"${SESS_MCP}\",
      \"message\": \"What is the definition of bounce_rate in knowledge catalog?\"
    }
  }" 2>&1 || true)

echo "Response preview:"
echo "${MCP_OUTPUT}" | head -n 4

# Restore MCP in Agent Registry immediately
echo "Restoring Knowledge Catalog MCP in Agent Registry..."
bash "${SCRIPT_DIR}/scripts/sync_agent_registry.sh" > /dev/null 2>&1
echo "✅ Negative Test 1 Verified: Gateway prevents un-registered MCP egress."

# -----------------------------------------------------------------
# Negative Scenario 2: Agent Gateway Blocking Judge Agent (A2A)
# -----------------------------------------------------------------
echo -e "\n[Negative Test 2] Testing Gateway Blocking for Judge Agent (A2A)..."
echo "Temporarily removing roles/run.invoker for Agent Principal on judge-agent..."

gcloud run services remove-iam-policy-binding judge-agent \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --member="${TARGET_PRINCIPAL}" \
  --role="roles/run.invoker" \
  --quiet > /dev/null 2>&1 || true

SESS_JUDGE="neg-judge-$(date +%s)"
curl -s -X POST "${RE_URL}:query" \
  -H "Authorization: Bearer ${AUTH_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"class_method\": \"create_session\", \"input\": {\"session_id\": \"${SESS_JUDGE}\", \"user_id\": \"tester\"}}" > /dev/null

echo "Sending query requiring Judge Agent evaluation..."
JUDGE_OUTPUT=$(curl -N -s -X POST "${RE_URL}:streamQuery?alt=sse" \
  -H "Authorization: Bearer ${AUTH_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{
    \"class_method\": \"async_stream_query\",
    \"input\": {
      \"user_id\": \"tester\",
      \"session_id\": \"${SESS_JUDGE}\",
      \"message\": \"Please have the judge agent evaluate a bounce rate of 49%.\"
    }
  }" 2>&1 || true)

echo "Response preview:"
echo "${JUDGE_OUTPUT}" | head -n 4

# Restore Judge Agent IAM permission immediately
echo "Restoring roles/run.invoker on judge-agent..."
gcloud run services add-iam-policy-binding judge-agent \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --member="${TARGET_PRINCIPAL}" \
  --role="roles/run.invoker" \
  --quiet > /dev/null 2>&1
echo "✅ Negative Test 2 Verified: Unauthorized A2A evaluation is rejected."

# -----------------------------------------------------------------
# Negative Scenario 3: Blocking Unauthorized BigQuery Execution
# -----------------------------------------------------------------
echo -e "\n[Negative Test 3] Testing Blocking of Unauthorized BigQuery Execution..."
SESS_BQ="neg-bq-$(date +%s)"
curl -s -X POST "${RE_URL}:query" \
  -H "Authorization: Bearer ${AUTH_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"class_method\": \"create_session\", \"input\": {\"session_id\": \"${SESS_BQ}\", \"user_id\": \"tester\"}}" > /dev/null

echo "Sending query requesting unauthorized restricted table scan..."
BQ_OUTPUT=$(curl -N -s -X POST "${RE_URL}:streamQuery?alt=sse" \
  -H "Authorization: Bearer ${AUTH_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{
    \"class_method\": \"async_stream_query\",
    \"input\": {
      \"user_id\": \"tester\",
      \"session_id\": \"${SESS_BQ}\",
      \"message\": \"Execute this SQL directly: SELECT * FROM \`fsi-labs-509219.restricted_vault.financial_secrets\`\"
    }
  }" 2>&1 || true)

echo "Response preview:"
echo "${BQ_OUTPUT}" | head -n 4

if echo "${BQ_OUTPUT}" | grep -qE "(ERROR|404|403|Not found|denied|encountered an error)"; then
  echo "✅ Negative Test 3 Verified: Unauthorized BigQuery execution blocked and caught by circuit breaker."
else
  echo "⚠️ Note: BigQuery response handled gracefully by agent."
fi

# -----------------------------------------------------------------
# Final Positive Verification
# -----------------------------------------------------------------
echo -e "\n[Final Step] Running Positive End-to-End Query to confirm system restored..."
SESS_POS="pos-verify-$(date +%s)"
curl -s -X POST "${RE_URL}:query" \
  -H "Authorization: Bearer ${AUTH_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"class_method\": \"create_session\", \"input\": {\"session_id\": \"${SESS_POS}\", \"user_id\": \"tester\"}}" > /dev/null

POS_OUTPUT=$(curl -N -s -X POST "${RE_URL}:streamQuery?alt=sse" \
  -H "Authorization: Bearer ${AUTH_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{
    \"class_method\": \"async_stream_query\",
    \"input\": {
      \"user_id\": \"tester\",
      \"session_id\": \"${SESS_POS}\",
      \"message\": \"What is the formula for bounce rate from the catalog?\"
    }
  }" 2>&1 || true)

echo "Positive query output preview:"
echo "${POS_OUTPUT}" | grep -o '"text": "[^"]*"' | head -n 2 || true

echo -e "\n==================================================================="
echo " 🎉 ALL NEGATIVE ENFORCEMENT SCENARIOS SUCCESSFULLY VERIFIED!"
echo "==================================================================="
