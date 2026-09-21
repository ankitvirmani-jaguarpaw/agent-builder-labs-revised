#!/usr/bin/env bash
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="${SCRIPT_DIR}"
PARENT_DIR="$(cd "${ROOT_DIR}/.." && pwd)"

# Parse optional command line flags
EXISTING_RE_ID=""
SKIP_INFRA=false
SKIP_VERIFY=false

while [[ $# -gt 0 ]]; do
  case $1 in
    --agent-engine-id|--re-id|--update)
      EXISTING_RE_ID="$2"
      shift 2
      ;;
    --skip-infra)
      SKIP_INFRA=true
      shift
      ;;
    --skip-verify)
      SKIP_VERIFY=true
      shift
      ;;
    *)
      if [[ -z "${EXISTING_RE_ID}" && "$1" =~ ^[0-9]+$ ]]; then
        EXISTING_RE_ID="$1"
        shift
      else
        echo "Unknown option: $1" >&2
        echo "Usage: ./deploy.sh [--agent-engine-id <ID>] [--skip-infra] [--skip-verify]" >&2
        exit 1
      fi
      ;;
  esac
done

echo "==================================================================="
echo " Turnkey Deployment: Data Analytics Agent with Agent Gateway"
echo "==================================================================="

# -----------------------------------------------------------------
# 1. Environment Discovery & Pre-flight
# -----------------------------------------------------------------
export PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
if [[ -z "${PROJECT_ID}" ]]; then
  echo "❌ ERROR: PROJECT_ID not found in environment or gcloud config." >&2
  exit 1
fi

export PROJECT_NUM="${PROJECT_NUM:-$(gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)")}"
export REGION="${REGION:-$(gcloud config get-value compute/region 2>/dev/null)}"
[[ -z "${REGION}" ]] && export REGION="us-central1"

echo "Project ID:       ${PROJECT_ID}"
echo "Project Number:   ${PROJECT_NUM}"
echo "Region:           ${REGION}"
if [[ -n "${EXISTING_RE_ID}" ]]; then
  echo "Target RE ID:     ${EXISTING_RE_ID} (In-place update)"
else
  echo "Deployment Mode:  New Reasoning Engine Instance"
fi

# -----------------------------------------------------------------
# 2. Provision Gateways, Model Armor, DLP & Authz Policies
# -----------------------------------------------------------------
if [[ "${SKIP_INFRA}" == false ]]; then
  echo -e "\n=== [Phase 1/5] Provisioning Gateways, DLP & Security Policies ==="
  bash "${ROOT_DIR}/scripts/setup_gateways_and_security.sh"
else
  echo -e "\n=== [Phase 1/5] Skipping Infrastructure Setup (--skip-infra passed) ==="
fi

# Ensure Gateway CA bundle exists
GATEWAY_CA_PATH="${ROOT_DIR}/gateway-ca.crt"
if [[ ! -f "${GATEWAY_CA_PATH}" || ! -s "${GATEWAY_CA_PATH}" ]]; then
  echo "Extracting Egress Gateway root CA certificate..."
  gcloud network-services agent-gateways describe fsi-agent-gateway-egress \
    --location="${REGION}" \
    --project="${PROJECT_ID}" \
    --format="value(agentGatewayCard.rootCertificates[0])" > "${GATEWAY_CA_PATH}"
  if [[ -f /etc/ssl/certs/ca-certificates.crt ]]; then
    cat /etc/ssl/certs/ca-certificates.crt >> "${GATEWAY_CA_PATH}"
  elif [[ -f /etc/pki/tls/certs/ca-bundle.crt ]]; then
    cat /etc/pki/tls/certs/ca-bundle.crt >> "${GATEWAY_CA_PATH}"
  fi
fi

# -----------------------------------------------------------------
# 3. Synchronize Agent Registry (A2MCP & A2A Endpoints)
# -----------------------------------------------------------------
echo -e "\n=== [Phase 2/5] Synchronizing Agent Registry ==="
bash "${ROOT_DIR}/scripts/sync_agent_registry.sh"

# Resolve live URLs
CATALOG_RUN_URL=$(gcloud run services describe knowledge-catalog-mcp --region="${REGION}" --project="${PROJECT_ID}" --format="value(status.url)" 2>/dev/null || true)
JUDGE_RUN_URL=$(gcloud run services describe judge-agent --region="${REGION}" --project="${PROJECT_ID}" --format="value(status.url)" 2>/dev/null || true)

CATALOG_MCP_URL="${CATALOG_RUN_URL}/sse"
JUDGE_AGENT_URL="${JUDGE_RUN_URL}"
AGW_INGRESS_URI="projects/${PROJECT_ID}/locations/${REGION}/agentGateways/fsi-agent-gateway-ingress"
AGW_EGRESS_URI="projects/${PROJECT_ID}/locations/${REGION}/agentGateways/fsi-agent-gateway-egress"

echo "Catalog MCP URL: ${CATALOG_MCP_URL}"
echo "Judge Agent URL: ${JUDGE_AGENT_URL}"
echo "Ingress Gateway: ${AGW_INGRESS_URI}"
echo "Egress Gateway:  ${AGW_EGRESS_URI}"

# -----------------------------------------------------------------
# 4. Generate Agent Configuration (.agent_engine_config.json & .env)
# -----------------------------------------------------------------
echo -e "\n=== [Phase 3/5] Generating Agent Configuration Files ==="

cat <<EOF > "${ROOT_DIR}/.agent_engine_config.json"
{
  "identity_type": "AGENT_IDENTITY",
  "agent_gateway_config": {
    "agent_to_anywhere_config": {
      "agent_gateway": "${AGW_EGRESS_URI}"
    },
    "client_to_agent_config": {
      "agent_gateway": "${AGW_INGRESS_URI}"
    }
  }
}
EOF
echo "✅ Generated ${ROOT_DIR}/.agent_engine_config.json"

cat <<EOF > "${ROOT_DIR}/.env"
GOOGLE_CLOUD_PROJECT=${PROJECT_ID}
GOOGLE_CLOUD_LOCATION=${REGION}
MODEL_NAME=gemini-2.5-flash-lite
CATALOG_MCP_URL=${CATALOG_MCP_URL}
JUDGE_AGENT_URL=${JUDGE_AGENT_URL}
AGENT_GATEWAY_INGRESS=${AGW_INGRESS_URI}
AGENT_GATEWAY_EGRESS=${AGW_EGRESS_URI}
SSL_CERT_FILE=/app/agents/data_analytics_agent_gateway/gateway-ca.crt
REQUESTS_CA_BUNDLE=/app/agents/data_analytics_agent_gateway/gateway-ca.crt
EOF
echo "✅ Generated ${ROOT_DIR}/.env"

# -----------------------------------------------------------------
# 5. Deploy Reasoning Engine via ADK CLI
# -----------------------------------------------------------------
echo -e "\n=== [Phase 4/5] Deploying Agent to Agent Engine via ADK CLI ==="

cd "${PARENT_DIR}"

DEPLOY_CMD=(adk deploy agent_engine \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --display_name="data_analytics_agent_gateway" \
  --otel_to_cloud)

if [[ -n "${EXISTING_RE_ID}" ]]; then
  DEPLOY_CMD+=(--agent_engine_id="${EXISTING_RE_ID}")
fi

DEPLOY_CMD+=(data_analytics_agent_gateway)

echo "Executing: ${DEPLOY_CMD[*]}"
DEPLOY_LOG="/tmp/adk_deploy_${PROJECT_ID}.log"
"${DEPLOY_CMD[@]}" 2>&1 | tee "${DEPLOY_LOG}"

# -----------------------------------------------------------------
# 6. Post-Deployment IAM Role Assignments for Agent Identity
# -----------------------------------------------------------------
echo -e "\n=== [Phase 5/5] Configuring IAM for Agent Identity Principal ==="

AUTH_TOKEN="$(gcloud auth print-access-token)"

if [[ -n "${EXISTING_RE_ID}" ]]; then
  DEPLOYED_RE_ID="${EXISTING_RE_ID}"
else
  # 1. Try extracting RE ID from ADK deployment log
  DEPLOYED_RE_ID=$(grep -oE 'reasoningEngines/[0-9]+' "${DEPLOY_LOG}" 2>/dev/null | awk -F'/' '{print $2}' | tail -n 1 || true)

  # 2. If not found in log, query Vertex AI API directly
  if [[ -z "${DEPLOYED_RE_ID}" ]]; then
    echo "Querying Vertex AI REST API to discover Reasoning Engine ID..."
    DEPLOYED_RE_ID=$(curl -s -H "Authorization: Bearer ${AUTH_TOKEN}" \
      "https://${REGION}-aiplatform.googleapis.com/v1/projects/${PROJECT_ID}/locations/${REGION}/reasoningEngines" \
      | python3 -c '
import sys, json
try:
    data = json.load(sys.stdin)
    engines = data.get("reasoningEngines", [])
    matching = [e for e in engines if e.get("displayName") == "data_analytics_agent_gateway"]
    if matching:
        matching.sort(key=lambda x: x.get("createTime", ""), reverse=True)
        print(matching[0]["name"].split("/")[-1])
    elif engines:
        engines.sort(key=lambda x: x.get("createTime", ""), reverse=True)
        print(engines[0]["name"].split("/")[-1])
except Exception:
    pass
' || true)
  fi
fi

if [[ -z "${DEPLOYED_RE_ID}" ]]; then
  echo "⚠️ Warning: Could not automatically detect deployed Reasoning Engine ID."
  echo "Please verify deployment output above."
else
  echo "Deployed Reasoning Engine ID: ${DEPLOYED_RE_ID}"

  # Resolve Organization ID dynamically
  ORG_ID=$(gcloud projects get-ancestors "${PROJECT_ID}" --filter="type:organization" --format="value(id)" 2>/dev/null || echo "788308946018")
  [[ -z "${ORG_ID}" ]] && ORG_ID="788308946018"

  TARGET_PRINCIPAL="principal://agents.global.org-${ORG_ID}.system.id.goog/resources/aiplatform/projects/${PROJECT_NUM}/locations/${REGION}/reasoningEngines/${DEPLOYED_RE_ID}"
  echo "Agent Identity Principal: ${TARGET_PRINCIPAL}"

  echo "Granting roles/networkservices.agentGatewayUser on ${AGW_EGRESS_URI}..."
  gcloud network-services agent-gateways add-iam-policy-binding fsi-agent-gateway-egress \
    --location="${REGION}" \
    --project="${PROJECT_ID}" \
    --member="${TARGET_PRINCIPAL}" \
    --role="roles/networkservices.agentGatewayUser" --quiet || true

  echo "Granting BigQuery and Telemetry roles (jobUser, dataViewer, cloudtrace, monitoring, logging)..."
  for role in \
    "roles/bigquery.jobUser" \
    "roles/bigquery.dataViewer" \
    "roles/cloudtrace.agent" \
    "roles/monitoring.metricWriter" \
    "roles/logging.logWriter"; do
    gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
      --member="${TARGET_PRINCIPAL}" \
      --role="${role}" \
      --condition=None --quiet || true
  done

  echo "Granting Cloud Run Invoker on downstream services..."
  for svc in "knowledge-catalog-mcp" "judge-agent"; do
    gcloud run services add-iam-policy-binding "${svc}" \
      --region="${REGION}" \
      --project="${PROJECT_ID}" \
      --member="${TARGET_PRINCIPAL}" \
      --role="roles/run.invoker" --quiet || true
  done

  echo "✅ IAM permissions successfully configured for Agent Identity."
fi

# -----------------------------------------------------------------
# 7. Verification & Audit Check
# -----------------------------------------------------------------
if [[ "${SKIP_VERIFY}" == false && -n "${DEPLOYED_RE_ID}" ]]; then
  echo -e "\n=== Executing Automated Verification Suite ==="
  bash "${ROOT_DIR}/verify.sh" --reasoning-engine-id "${DEPLOYED_RE_ID}"
else
  echo -e "\nVerification skipped or deferred."
  if [[ -n "${DEPLOYED_RE_ID}" ]]; then
    echo "To verify manually anytime, run:"
    echo "  ./data_analytics_agent_gateway/verify.sh --reasoning-engine-id ${DEPLOYED_RE_ID}"
  fi
fi

echo -e "\n==================================================================="
echo " 🎉 Turnkey Deployment and Setup Finished Successfully!"
echo "==================================================================="
