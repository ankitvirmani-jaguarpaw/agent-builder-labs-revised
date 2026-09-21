#!/usr/bin/env bash
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
CFG_DIR="${ROOT_DIR}/cfg"

# 1. Environment Discovery
export PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
if [[ -z "${PROJECT_ID}" ]]; then
  echo "❌ ERROR: PROJECT_ID could not be determined. Set PROJECT_ID or configure gcloud." >&2
  exit 1
fi

export PROJECT_NUM="${PROJECT_NUM:-$(gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)")}"
export REGION="${REGION:-$(gcloud config get-value compute/region 2>/dev/null)}"
[[ -z "${REGION}" ]] && export REGION="us-central1"

echo "==================================================================="
echo " Setting Up Gateways, Security Policies & Templates"
echo " Project ID    : ${PROJECT_ID}"
echo " Project Number: ${PROJECT_NUM}"
echo " Region        : ${REGION}"
echo "==================================================================="

AUTH_TOKEN="$(gcloud auth print-access-token)"

# Helper function to substitute environment variables in config files
substitute_vars() {
  local src="$1"
  local dst="$2"
  python3 -c "
import os, sys
with open(sys.argv[1], 'r') as f:
    content = f.read()
content = content.replace('\${PROJECT_ID}', os.environ['PROJECT_ID'])
content = content.replace('\${PROJECT_NUM}', os.environ['PROJECT_NUM'])
content = content.replace('\${REGION}', os.environ['REGION'])
with open(sys.argv[2], 'w') as f:
    f.write(content)
" "${src}" "${dst}"
}

# -----------------------------------------------------------------
# 2. DLP Templates for Ingress/Egress Inspection and De-identification
# -----------------------------------------------------------------
echo -e "\n--- Step 1: Checking/Creating DLP Templates ---"

# 1a. Inspect Template (US SSN detection)
INSPECT_STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer ${AUTH_TOKEN}" \
  -H "x-goog-user-project: ${PROJECT_ID}" \
  "https://dlp.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/inspectTemplates/agw-ssn-inspect-template")

if [[ "${INSPECT_STATUS}" == "200" ]]; then
  echo "✅ DLP Inspect Template \"agw-ssn-inspect-template\" already exists."
else
  echo "Creating DLP Inspect Template \"agw-ssn-inspect-template\"..."
  curl -s -f -X POST \
    -H "Authorization: Bearer ${AUTH_TOKEN}" \
    -H "Content-Type: application/json" \
    -H "x-goog-user-project: ${PROJECT_ID}" \
    "https://dlp.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/inspectTemplates?templateId=agw-ssn-inspect-template" \
    -d @"${CFG_DIR}/agw-ssn-inspect-template.json" > /dev/null
  echo "✅ Created DLP Inspect Template."
fi

# 1b. De-identify Template (Redact SSN)
DEIDENTIFY_STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer ${AUTH_TOKEN}" \
  -H "x-goog-user-project: ${PROJECT_ID}" \
  "https://dlp.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/deidentifyTemplates/agw-ssn-redaction-template")

if [[ "${DEIDENTIFY_STATUS}" == "200" ]]; then
  echo "✅ DLP De-identify Template \"agw-ssn-redaction-template\" already exists."
else
  echo "Creating DLP De-identify Template \"agw-ssn-redaction-template\"..."
  curl -s -f -X POST \
    -H "Authorization: Bearer ${AUTH_TOKEN}" \
    -H "Content-Type: application/json" \
    -H "x-goog-user-project: ${PROJECT_ID}" \
    "https://dlp.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/deidentifyTemplates?templateId=agw-ssn-redaction-template" \
    -d @"${CFG_DIR}/agw-ssn-redaction-template.json" > /dev/null
  echo "✅ Created DLP De-identify Template."
fi

# -----------------------------------------------------------------
# 3. Model Armor Templates (Request Prompt-Injection & Response DLP)
# -----------------------------------------------------------------
echo -e "\n--- Step 2: Checking/Creating Model Armor Templates ---"

# 2a. Request Template
REQ_TMPL_STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer ${AUTH_TOKEN}" \
  -H "x-goog-user-project: ${PROJECT_ID}" \
  "https://modelarmor.${REGION}.rep.googleapis.com/v1/projects/${PROJECT_ID}/locations/${REGION}/templates/agw-request-template")

if [[ "${REQ_TMPL_STATUS}" == "200" ]]; then
  echo "✅ Model Armor Request Template \"agw-request-template\" already exists."
else
  echo "Creating Model Armor Request Template \"agw-request-template\"..."
  curl -s -f -X POST \
    -H "Authorization: Bearer ${AUTH_TOKEN}" \
    -H "Content-Type: application/json" \
    -H "x-goog-user-project: ${PROJECT_ID}" \
    "https://modelarmor.${REGION}.rep.googleapis.com/v1/projects/${PROJECT_ID}/locations/${REGION}/templates?templateId=agw-request-template" \
    -d @"${CFG_DIR}/agw-request-template.json" > /dev/null
  echo "✅ Created Model Armor Request Template."
fi

# 2b. Response Template
RESP_TMPL_STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer ${AUTH_TOKEN}" \
  -H "x-goog-user-project: ${PROJECT_ID}" \
  "https://modelarmor.${REGION}.rep.googleapis.com/v1/projects/${PROJECT_ID}/locations/${REGION}/templates/agw-response-template")

if [[ "${RESP_TMPL_STATUS}" == "200" ]]; then
  echo "✅ Model Armor Response Template \"agw-response-template\" already exists."
else
  echo "Creating Model Armor Response Template \"agw-response-template\"..."
  substitute_vars "${CFG_DIR}/agw-response-template.json" "/tmp/agw-response-template-rendered.json"
  curl -s -f -X POST \
    -H "Authorization: Bearer ${AUTH_TOKEN}" \
    -H "Content-Type: application/json" \
    -H "x-goog-user-project: ${PROJECT_ID}" \
    "https://modelarmor.${REGION}.rep.googleapis.com/v1/projects/${PROJECT_ID}/locations/${REGION}/templates?templateId=agw-response-template" \
    -d @"/tmp/agw-response-template-rendered.json" > /dev/null
  rm -f "/tmp/agw-response-template-rendered.json"
  echo "✅ Created Model Armor Response Template."
fi

# -----------------------------------------------------------------
# 4. Agent Gateways Provisioning (Ingress & Egress)
# -----------------------------------------------------------------
echo -e "\n--- Step 3: Provisioning Agent Gateways ---"

# 4a. Ingress Gateway (CLIENT_TO_AGENT)
if gcloud network-services agent-gateways describe fsi-agent-gateway-ingress \
  --location="${REGION}" --project="${PROJECT_ID}" > /dev/null 2>&1; then
  echo "✅ Ingress Gateway \"fsi-agent-gateway-ingress\" exists."
else
  echo "Importing Ingress Gateway \"fsi-agent-gateway-ingress\"..."
  gcloud network-services agent-gateways import fsi-agent-gateway-ingress \
    --location="${REGION}" \
    --project="${PROJECT_ID}" \
    --source="${CFG_DIR}/fsi-agent-gateway-ingress.yaml"
  echo "✅ Created Ingress Gateway \"fsi-agent-gateway-ingress\"."
fi

# 4b. Egress Gateway (AGENT_TO_ANYWHERE)
if gcloud network-services agent-gateways describe fsi-agent-gateway-egress \
  --location="${REGION}" --project="${PROJECT_ID}" > /dev/null 2>&1; then
  echo "✅ Egress Gateway \"fsi-agent-gateway-egress\" exists."
else
  echo "Importing Egress Gateway \"fsi-agent-gateway-egress\"..."
  substitute_vars "${CFG_DIR}/fsi-agent-gateway-egress.yaml" "/tmp/fsi-agent-gateway-egress-rendered.yaml"
  gcloud network-services agent-gateways import fsi-agent-gateway-egress \
    --location="${REGION}" \
    --project="${PROJECT_ID}" \
    --source="/tmp/fsi-agent-gateway-egress-rendered.yaml"
  rm -f "/tmp/fsi-agent-gateway-egress-rendered.yaml"
  echo "✅ Created Egress Gateway \"fsi-agent-gateway-egress\"."
fi

# -----------------------------------------------------------------
# 5. Service Extensions Service Account Permissions
# -----------------------------------------------------------------
echo -e "\n--- Step 4: Configuring Service Extensions Service Account Permissions ---"
DEP_SA="service-${PROJECT_NUM}@gcp-sa-dep.iam.gserviceaccount.com"
echo "Project DEP Service Account: ${DEP_SA}"

# Also discover tenant serviceExtensionsServiceAccount from the egress gateway card
GATEWAY_TENANT_SA=$(gcloud network-services agent-gateways describe fsi-agent-gateway-egress \
  --location="${REGION}" --project="${PROJECT_ID}" \
  --format="value(agentGatewayCard.serviceExtensionsServiceAccount)" 2>/dev/null || true)
if [[ -n "${GATEWAY_TENANT_SA}" ]]; then
  echo "Gateway Tenant Service Extensions SA: ${GATEWAY_TENANT_SA}"
fi

for sa in "${DEP_SA}" "${GATEWAY_TENANT_SA}"; do
  if [[ -n "${sa}" ]]; then
    for role in "roles/modelarmor.calloutUser" "roles/serviceusage.serviceUsageConsumer" "roles/modelarmor.user"; do
      gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
        --member="serviceAccount:${sa}" \
        --role="${role}" \
        --condition=None --quiet > /dev/null 2>&1 || true
    done
    echo "✅ Granted Model Armor and Service Usage roles to ${sa}."
  fi
done

# -----------------------------------------------------------------
# 6. Service Extensions (Model Armor Authz & IAP Egress Authz)
# -----------------------------------------------------------------
echo -e "\n--- Step 5: Provisioning Service Extensions (Authz Extensions) ---"

# 6a. Model Armor Authz Extension
substitute_vars "${CFG_DIR}/fsi-agent-gateway-ma-authz.yaml" "/tmp/fsi-agent-gateway-ma-authz-rendered.yaml"
echo "Importing/Updating Model Armor Authz Extension \"fsi-agent-gateway-ma-authz\"..."
gcloud beta service-extensions authz-extensions import fsi-agent-gateway-ma-authz \
  --source="/tmp/fsi-agent-gateway-ma-authz-rendered.yaml" \
  --location="${REGION}" \
  --project="${PROJECT_ID}" --quiet
rm -f "/tmp/fsi-agent-gateway-ma-authz-rendered.yaml"
echo "✅ Model Armor Authz Extension ready."

# 6b. IAP Authz Extension (Dry-Run / Token Inspection for Egress)
if gcloud beta service-extensions authz-extensions describe fsi-agent-gateway-egress-svc-ext-authz-iap-dryrun \
  --location="${REGION}" --project="${PROJECT_ID}" > /dev/null 2>&1; then
  echo "✅ IAP Authz Extension \"fsi-agent-gateway-egress-svc-ext-authz-iap-dryrun\" exists."
else
  echo "Importing IAP Authz Extension \"fsi-agent-gateway-egress-svc-ext-authz-iap-dryrun\"..."
  gcloud beta service-extensions authz-extensions import fsi-agent-gateway-egress-svc-ext-authz-iap-dryrun \
    --source="${CFG_DIR}/fsi-agent-gateway-egress-svc-ext-authz-iap-dryrun.yaml" \
    --location="${REGION}" \
    --project="${PROJECT_ID}" --quiet
  echo "✅ Created IAP Authz Extension."
fi

# -----------------------------------------------------------------
# 7. Authz Policies (Ingress Model Armor Policy & Egress IAP Policy)
# -----------------------------------------------------------------
echo -e "\n--- Step 6: Provisioning Authz Policies ---"

# 7a. Ingress Model Armor Authz Policy
INGRESS_POLICY_STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer ${AUTH_TOKEN}" \
  -H "x-goog-user-project: ${PROJECT_ID}" \
  "https://networksecurity.googleapis.com/v1alpha1/projects/${PROJECT_ID}/locations/${REGION}/authzPolicies/fsi-agent-gateway-ingress-ma-policy")

if [[ "${INGRESS_POLICY_STATUS}" == "200" ]]; then
  echo "✅ Ingress Authz Policy \"fsi-agent-gateway-ingress-ma-policy\" already exists."
else
  echo "Creating Ingress Authz Policy \"fsi-agent-gateway-ingress-ma-policy\"..."
  substitute_vars "${CFG_DIR}/fsi-agent-gateway-ingress-ma-policy.json" "/tmp/fsi-agent-gateway-ingress-ma-policy-rendered.json"
  CREATE_INGRESS_RESP=$(curl -s -w "\n%{http_code}" -X POST \
    -H "Authorization: Bearer ${AUTH_TOKEN}" \
    -H "Content-Type: application/json" \
    "https://networksecurity.googleapis.com/v1alpha1/projects/${PROJECT_ID}/locations/${REGION}/authzPolicies?authz_policy_id=fsi-agent-gateway-ingress-ma-policy" \
    -d @"/tmp/fsi-agent-gateway-ingress-ma-policy-rendered.json")
  rm -f "/tmp/fsi-agent-gateway-ingress-ma-policy-rendered.json"
  INGRESS_RESP_CODE=$(echo "${CREATE_INGRESS_RESP}" | tail -n 1)
  if [[ "${INGRESS_RESP_CODE}" == "200" || "${INGRESS_RESP_CODE}" == "201" ]]; then
    echo "✅ Created Ingress Model Armor Authz Policy."
  else
    echo "⚠️ Response (${INGRESS_RESP_CODE}): $(echo "${CREATE_INGRESS_RESP}" | sed '$d')"
  fi
fi

# 7b. Egress IAP Authz Policy
EGRESS_POLICY_STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer ${AUTH_TOKEN}" \
  -H "x-goog-user-project: ${PROJECT_ID}" \
  "https://networksecurity.googleapis.com/v1alpha1/projects/${PROJECT_ID}/locations/${REGION}/authzPolicies/fsi-agent-gateway-egress-iap-policy")

if [[ "${EGRESS_POLICY_STATUS}" == "200" ]]; then
  echo "✅ Egress Authz Policy \"fsi-agent-gateway-egress-iap-policy\" already exists."
else
  echo "Creating Egress Authz Policy \"fsi-agent-gateway-egress-iap-policy\"..."
  substitute_vars "${CFG_DIR}/fsi-agent-gateway-egress-iap-policy.json" "/tmp/fsi-agent-gateway-egress-iap-policy-rendered.json"
  CREATE_EGRESS_RESP=$(curl -s -w "\n%{http_code}" -X POST \
    -H "Authorization: Bearer ${AUTH_TOKEN}" \
    -H "Content-Type: application/json" \
    "https://networksecurity.googleapis.com/v1alpha1/projects/${PROJECT_ID}/locations/${REGION}/authzPolicies?authz_policy_id=fsi-agent-gateway-egress-iap-policy" \
    -d @"/tmp/fsi-agent-gateway-egress-iap-policy-rendered.json")
  rm -f "/tmp/fsi-agent-gateway-egress-iap-policy-rendered.json"
  EGRESS_RESP_CODE=$(echo "${CREATE_EGRESS_RESP}" | tail -n 1)
  if [[ "${EGRESS_RESP_CODE}" == "200" || "${EGRESS_RESP_CODE}" == "201" ]]; then
    echo "✅ Created Egress IAP Authz Policy."
  else
    echo "⚠️ Response (${EGRESS_RESP_CODE}): $(echo "${CREATE_EGRESS_RESP}" | sed '$d')"
  fi
fi

# -----------------------------------------------------------------
# 8. Extract Egress Gateway CA Certificate & Append System Roots
# -----------------------------------------------------------------
echo -e "\n--- Step 7: Generating Gateway CA Certificate Bundle ---"
GATEWAY_CERT_PATH="${ROOT_DIR}/gateway-ca.crt"
echo "Extracting root certificate from \"fsi-agent-gateway-egress\"..."
gcloud network-services agent-gateways describe fsi-agent-gateway-egress \
  --location="${REGION}" \
  --project="${PROJECT_ID}" \
  --format="value(agentGatewayCard.rootCertificates[0])" > "${GATEWAY_CERT_PATH}"

# Concatenate certifi and system public CA certificates (ensuring GTS Root R1 for Google APIs is present)
python3 -c "
import certifi
with open(certifi.where(), 'r') as f_in, open('${GATEWAY_CERT_PATH}', 'a') as f_out:
    f_out.write('\n' + f_in.read())
"
echo "✅ Gateway CA bundle generated at: ${GATEWAY_CERT_PATH} ($(wc -l < "${GATEWAY_CERT_PATH}") lines)"

echo -e "\n==================================================================="
echo " 🎉 Gateway & Security Infrastructure Provisioning Complete!"
echo "==================================================================="
