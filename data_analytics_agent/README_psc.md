Layer
Resource
Direction
PSC endpoint for Google APIs
Global address + forwarding rule + private DNS zone
Your VPC → Google APIs, privately
PSC interface
Subnet + network attachment + DNS peering
Agent Engine tenant → your VPC

The interface gets the agent into your VPC. The endpoint + DNS is what it then uses to reach BigQuery privately. Build them in that order — the DNS zone must exist before Vertex peers to it.

Step 0 — Set every variable once
Everything below derives from these. Run this block first in every new shell.
# --- Core, resolved dynamically -------------------------------------------
export PROJECT_ID=$(gcloud config get-value project)
export PROJECT_NUMBER=$(gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)")

export REGION=$(gcloud config get-value compute/region 2>/dev/null)
[ -z "${REGION}" ] && export REGION="us-central1"

# ADK / Vertex AI read these names specifically
export GOOGLE_CLOUD_PROJECT="${PROJECT_ID}"
export GOOGLE_CLOUD_LOCATION="${REGION}"
export GOOGLE_GENAI_USE_VERTEXAI="true"

# --- Networking -----------------------------------------------------------
export NETWORK_NAME="default"
export PSC_IP="10.0.0.5"                      # Unused IP, must not overlap any subnet
export PSC_SUBNET_NAME="psc-intf-subnet"
export PSC_SUBNET_RANGE="10.0.1.0/28"         # Dedicated to the PSC interface
export NETWORK_ATTACHMENT_NAME="psc-agent-attachment"
export DNS_ZONE_NAME="googleapis-private-zone"

# --- Deployment -----------------------------------------------------------
export AGENT_DIR="data_analytics_agent_psc"
export DISPLAY_NAME="data_analytics_nogateway_psc"

# --- Vertex AI service agent (P4SA) ---------------------------------------
export VERTEX_SA="service-${PROJECT_NUMBER}@gcp-sa-aiplatform.iam.gserviceaccount.com"

cat <<INFO
Project ID:         ${PROJECT_ID}
Project Number:     ${PROJECT_NUMBER}
Region:             ${REGION}
VPC Network:        ${NETWORK_NAME}
PSC IP:             ${PSC_IP}
PSC Subnet:         ${PSC_SUBNET_NAME} (${PSC_SUBNET_RANGE})
Network Attachment: ${NETWORK_ATTACHMENT_NAME}
Vertex AI SA:       ${VERTEX_SA}
Display Name:       ${DISPLAY_NAME}
INFO
Confirm PROJECT_ID and PROJECT_NUMBER are non-empty before continuing.

Step 1 — Enable the APIs
All of them, up front. The original runbook enabled dns.googleapis.com mid-stream, which fails the DNS commands if it wasn't already on.
gcloud services enable \
  compute.googleapis.com \
  dns.googleapis.com \
  aiplatform.googleapis.com \
  --project="${PROJECT_ID}"

Step 2 — Create the Vertex AI service agent identity
Must come before any IAM grant to that account. The original granted roles to service-<NUM>@gcp-sa-aiplatform... before the identity existed, which fails with Service account does not exist.
gcloud beta services identity create \
  --service=aiplatform.googleapis.com \
  --project="${PROJECT_ID}"

# Confirm it resolves
gcloud iam service-accounts describe "${VERTEX_SA}" --project="${PROJECT_ID}" 2>/dev/null \
  || echo "NOTE: P4SA is Google-managed and may not be describable; proceed anyway."

Step 3 — PSC endpoint for Google APIs
Internal entry point routing to Google APIs without traversing the public internet.
# Reserve the internal IP
gcloud compute addresses create psc-google-apis-ip \
  --global \
  --purpose=PRIVATE_SERVICE_CONNECT \
  --addresses="${PSC_IP}" \
  --network="${NETWORK_NAME}" \
  --project="${PROJECT_ID}"

# Forwarding rule targeting the all-apis bundle
gcloud compute forwarding-rules create pscapis \
  --global \
  --network="${NETWORK_NAME}" \
  --address=psc-google-apis-ip \
  --target-google-apis-bundle=all-apis \
  --project="${PROJECT_ID}"
Verify:
gcloud compute forwarding-rules describe pscapis --global \
  --project="${PROJECT_ID}" --format="value(name,IPAddress,target)"

Step 4 — Private DNS zone and records
Overrides public routing for Google APIs inside your VPC.
gcloud dns managed-zones create "${DNS_ZONE_NAME}" \
  --description="Private DNS zone for Google APIs over PSC" \
  --dns-name="googleapis.com." \
  --visibility=private \
  --networks="${NETWORK_NAME}" \
  --project="${PROJECT_ID}"

# Base domain
gcloud dns record-sets create googleapis.com. \
  --zone="${DNS_ZONE_NAME}" \
  --type=A \
  --ttl=300 \
  --rrdatas="${PSC_IP}" \
  --project="${PROJECT_ID}"

# All subdomains — catches bigquery.googleapis.com
# NOTE: the wildcard MUST be quoted or your shell expands it against local files
gcloud dns record-sets create "*.googleapis.com." \
  --zone="${DNS_ZONE_NAME}" \
  --type=A \
  --ttl=300 \
  --rrdatas="${PSC_IP}" \
  --project="${PROJECT_ID}"
Verify both records point at ${PSC_IP}:
gcloud dns record-sets list --zone="${DNS_ZONE_NAME}" \
  --project="${PROJECT_ID}" --format="table(name,type,rrdatas)"

Step 5 — PSC interface subnet and network attachment
The subnet must be in the same region you deploy the agent to.
gcloud compute networks subnets create "${PSC_SUBNET_NAME}" \
  --project="${PROJECT_ID}" \
  --network="${NETWORK_NAME}" \
  --region="${REGION}" \
  --range="${PSC_SUBNET_RANGE}" \
  --enable-private-ip-google-access

gcloud compute network-attachments create "${NETWORK_ATTACHMENT_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --subnets="${PSC_SUBNET_NAME}" \
  --connection-preference=ACCEPT_AUTOMATIC
IP range constraint: Vertex cannot reach certain ranges from the PSC interface. Check the subnetwork IP range requirements in the PSC-I docs before picking something other than 10.0.1.0/28.
Verify:
gcloud compute network-attachments describe "${NETWORK_ATTACHMENT_NAME}" \
  --region="${REGION}" --project="${PROJECT_ID}" \
  --format="value(name,connectionPreference,subnetworks[0])"

Step 6 — Grant the Vertex AI service agent its roles
Now that the identity exists and the resources it must touch exist.
# Lets the tenant patch the network attachment allowlist
# (needs compute.networkAttachments.get + .update)
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${VERTEX_SA}" \
  --role="roles/compute.networkAdmin" \
  --condition=None

# Lets the tenant VPC peer to the private DNS zone from Step 4
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${VERTEX_SA}" \
  --role="roles/dns.peer" \
  --condition=None

# Verify
gcloud projects get-iam-policy "${PROJECT_ID}" \
  --flatten="bindings[].members" \
  --filter="bindings.members:${VERTEX_SA}" \
  --format="table(bindings.role)"
roles/dns.peer must be granted in the project that owns the DNS record. Same project here, but if the zone lives elsewhere, grant it there instead.
If compute.networkAdmin is too broad for your org, substitute a custom role carrying only compute.networkAttachments.get and compute.networkAttachments.update.

Step 7 — Copy the agent folder
Run from the repo base directory. This creates the PSC copy without touching the original.
cd ~/agent-builder-labs-revised

cp -a data_analytics_agent/. data_analytics_agent_psc/

Step 8 — Generate .agent_engine_config.json
Still in the base directory — do not cd into the agent folder. The original runbook cd'd in here and then never came back out, so the final adk deploy ... data_analytics_agent_psc resolved to a nonexistent nested path.
Resolve the VPC name back from the attachment, so the config is self-consistent with what you actually created:
export SUBNET_URL=$(gcloud compute network-attachments describe "${NETWORK_ATTACHMENT_NAME}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --format="value(subnetworks[0])")

export NETWORK_URL=$(gcloud compute networks subnets describe "${SUBNET_URL}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --format="value(network)" 2>/dev/null)

export RESOLVED_NETWORK=$(basename "${NETWORK_URL}")
[ -z "${RESOLVED_NETWORK}" ] && export RESOLVED_NETWORK="${NETWORK_NAME}"

echo "Resolved Project:    ${PROJECT_ID}"
echo "Resolved Region:     ${REGION}"
echo "Resolved Attachment: ${NETWORK_ATTACHMENT_NAME}"
echo "Resolved VPC:        ${RESOLVED_NETWORK}"
Write the config:
cat <<EOF > ".agent_engine_config.json"
{
  "psc_interface_config": {
    "network_attachment": "projects/${PROJECT_ID}/regions/${REGION}/networkAttachments/${NETWORK_ATTACHMENT_NAME}",
    "dns_peering_configs": [
      {
        "domain": "googleapis.com.",
        "target_project": "${PROJECT_ID}",
        "target_network": "${RESOLVED_NETWORK}"
      }
    ]
  }
}
EOF

cat ".agent_engine_config.json"
dns_peering_configs is not optional in practice. Vertex does not auto-select a zone — omit it and DNS resolution silently falls back to public.

Step 9 — Point the agent at its Cloud Run dependencies
CATALOG_BASE_URL=$(gcloud run services describe knowledge-catalog-mcp \
  --project="${PROJECT_ID}" --region="${REGION}" --format='value(status.url)')

JUDGE_BASE_URL=$(gcloud run services describe judge-agent \
  --project="${PROJECT_ID}" --region="${REGION}" --format='value(status.url)')

cat <<EOF > ".env"
GOOGLE_CLOUD_PROJECT=${PROJECT_ID}
GOOGLE_CLOUD_LOCATION=${REGION}
CATALOG_MCP_URL=${CATALOG_BASE_URL}/sse
JUDGE_AGENT_URL=${JUDGE_BASE_URL}
EOF

cat ".env"
Skip this step if your PSC variant doesn't call the catalog or judge.

Step 10 — Deploy
From the base directory. GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION were exported back in Step 0 — the original runbook referenced them here without ever setting them, so the deploy went out with empty --project/--region.
pwd   # must be the repo base, NOT inside ${AGENT_DIR}

adk deploy agent_engine \
  --project="${GOOGLE_CLOUD_PROJECT}" \
  --region="${GOOGLE_CLOUD_LOCATION}" \
  --display_name="${DISPLAY_NAME}" \
  --otel_to_cloud \
  .


Playground query:
Confirm it egressed privately:
gcloud logging read \
  'resource.type="bigquery_project" AND protoPayload.serviceName="bigquery.googleapis.com"' \
  --project="${PROJECT_ID}" \
  --limit=5 \
  --format="yaml(timestamp, protoPayload.authenticationInfo.principalSubject, protoPayload.requestMetadata.callerIp, protoPayload.requestMetadata.callerNetwork, protoPayload.methodName)"
callerIp should be an internal RFC 1918 address from ${PSC_SUBNET_RANGE}, and callerNetwork should name your VPC.



