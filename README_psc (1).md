# Deploy the Data Analytics Agent behind Private Service Connect (PSC) - Option 1 (Global Endpoint)

| Layer | Resource | Direction |
| :---- | :---- | :---- |
| **PSC endpoint for Google APIs** | Global address + forwarding rule + private DNS zone | Your VPC → Google APIs (BigQuery & Vertex AI), privately |
| **PSC interface** | Subnet + network attachment + DNS peering | Agent Engine tenant → your VPC |

The interface gets the agent *into* your VPC. The PSC endpoint (`10.0.0.5`) + DNS is what it then uses to reach BigQuery (`bigquery.googleapis.com`) and Vertex AI (`aiplatform.googleapis.com`) privately under a single `*.googleapis.com` TLS certificate.

---

## Step 0 — Set variables once

Run from your shell:

```sh
export PROJECT_ID=$(gcloud config get-value project)
export PROJECT_NUM=$(gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)")

# Compute & VPC regional placement for PSC Subnet & Network Attachment
export REGION=$(gcloud config get-value compute/region 2>/dev/null)
[ -z "${REGION}" ] && export REGION="us-central1"

# ADK & Vertex AI Model configuration for Gemini 3.8 Flash (Option 1: Global Endpoint)
export GOOGLE_CLOUD_PROJECT="${PROJECT_ID}"
export GOOGLE_CLOUD_LOCATION="global"
export MODEL_NAME="gemini-3.8-flash"
export GOOGLE_GENAI_USE_VERTEXAI="true"

# VPC and PSC network naming
export NETWORK_NAME="default"
export PSC_SUBNET_NAME="psc-agent-engine-subnet"
export PSC_SUBNET_RANGE="10.0.1.0/28"
export NETWORK_ATTACHMENT_NAME="agent-engine-psc-attachment"
export DNS_ZONE_NAME="psc-google-apis-zone"
export PSC_IP="10.0.0.5"

# Vertex AI service agent
export VERTEX_SA="service-${PROJECT_NUM}@gcp-sa-aiplatform.iam.gserviceaccount.com"

# Folder names
export AGENT_DIR="data_analytics_agent_psc"
export DISPLAY_NAME="data_analytics_psc"
```

Verify your environment variables:

```sh
echo "Project:      ${PROJECT_ID} (${PROJECT_NUM})"
echo "Compute:      ${REGION}"
echo "Model/Loc:    ${MODEL_NAME} (${GOOGLE_CLOUD_LOCATION})"
echo "Vertex SA:    ${VERTEX_SA}"
```

---

## Step 1 — Enable required APIs

```sh
gcloud services enable \
  compute.googleapis.com \
  dns.googleapis.com \
  aiplatform.googleapis.com \
  bigquery.googleapis.com \
  dataplex.googleapis.com \
  --project="${PROJECT_ID}"
```

---

## Step 2 — Ensure the Vertex AI service agent exists

```sh
gcloud beta services identity create \
  --service=aiplatform.googleapis.com \
  --project="${PROJECT_ID}"
```

---

## Step 3 — Reserve the internal IP and create the PSC forwarding rule

The forwarding rule uses `--target-google-apis-bundle=all-apis`. This publishes the private endpoint for BigQuery (`bigquery.googleapis.com`) and Vertex AI (`aiplatform.googleapis.com`).

```sh
# Reserve the IP inside your VPC
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
```

Verify the forwarding rule:

```sh
gcloud compute forwarding-rules describe pscapis --global \
  --project="${PROJECT_ID}" --format="value(name,IPAddress,target)"
```

---

## Step 4 — Private DNS zone and records

Overrides public routing for Google APIs inside your VPC:

```sh
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

# Single-level wildcard matches bigquery.googleapis.com and aiplatform.googleapis.com cleanly
gcloud dns record-sets create "*.googleapis.com." \
  --zone="${DNS_ZONE_NAME}" \
  --type=A \
  --ttl=300 \
  --rrdatas="${PSC_IP}" \
  --project="${PROJECT_ID}"
```

Verify DNS records:

```sh
gcloud dns record-sets list --zone="${DNS_ZONE_NAME}" \
  --project="${PROJECT_ID}" --format="table(name,type,rrdatas)"
```

---

## Step 5 — PSC interface subnet and network attachment

The subnet must be in the compute region (`${REGION}` = `us-central1`):

```sh
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
```

Verify network attachment:

```sh
gcloud compute network-attachments describe "${NETWORK_ATTACHMENT_NAME}" \
  --region="${REGION}" --project="${PROJECT_ID}" \
  --format="value(name,connectionPreference,subnetworks[0])"
```

---

## Step 6 — Grant Vertex AI service agent IAM permissions

```sh
# Allows tenant to patch the network attachment allowlist
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${VERTEX_SA}" \
  --role="roles/compute.networkAdmin" \
  --condition=None

# Allows tenant VPC to peer to the private DNS zone from Step 4
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${VERTEX_SA}" \
  --role="roles/dns.peer" \
  --condition=None
```

---

## Step 7 — Copy the agent folder

Run from the **repo base directory**:

```sh
cd ~/agent-builder-labs-revised

cp -a data_analytics_agent/. "${AGENT_DIR}/"
```

---

## Step 8 — Generate `.agent_engine_config.json`

Still from the repo base directory (do not `cd` into `${AGENT_DIR}`):

```sh
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

cat <<EOF > "${AGENT_DIR}/.agent_engine_config.json"
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

cat "${AGENT_DIR}/.agent_engine_config.json"
```

---

## Step 9 — Point the agent at Cloud Run dependencies & set Gemini 3.8 Flash (Global)

Write the runtime `.env` directly into `${AGENT_DIR}/.env`:

```sh
CATALOG_BASE_URL=$(gcloud run services describe knowledge-catalog-mcp \
  --project="${PROJECT_ID}" --region="${REGION}" --format='value(status.url)')

JUDGE_BASE_URL=$(gcloud run services describe judge-agent \
  --project="${PROJECT_ID}" --region="${REGION}" --format='value(status.url)')

cat <<EOF > "${AGENT_DIR}/.env"
GOOGLE_CLOUD_PROJECT=${PROJECT_ID}
GOOGLE_CLOUD_LOCATION=global
MODEL_NAME=gemini-3.8-flash
CATALOG_MCP_URL=${CATALOG_BASE_URL}/sse
JUDGE_AGENT_URL=${JUDGE_BASE_URL}
ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=true
OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true
EOF

cat "${AGENT_DIR}/.env"
```

---

## Step 10 — Deploy to Agent Engine

From the repo base directory:

```sh
pwd   # must be ~/agent-builder-labs-revised, NOT inside ${AGENT_DIR}

adk deploy agent_engine \
  --project="${GOOGLE_CLOUD_PROJECT}" \
  --region="${REGION}" \
  --display_name="${DISPLAY_NAME}" \
  --otel_to_cloud \
  "${AGENT_DIR}"
```

---

## Step 11 — Verify Private Egress in Cloud Logging

After issuing a test analytical query to the deployed Reasoning Engine, verify that BigQuery and Vertex AI API traffic originated from the internal PSC subnet range (`10.0.1.x`) rather than public IP egress:

```sh
gcloud logging read \
  'resource.type="bigquery_project" AND protoPayload.serviceName="bigquery.googleapis.com"' \
  --project="${PROJECT_ID}" \
  --limit=5 \
  --format="yaml(timestamp, protoPayload.authenticationInfo.principalSubject, protoPayload.requestMetadata.callerIp, protoPayload.requestMetadata.callerNetwork, protoPayload.methodName)"
```

- **`callerIp`**: Must match an RFC 1918 address from `${PSC_SUBNET_RANGE}` (`10.0.1.x`).
- **`callerNetwork`**: Must display `projects/${PROJECT_NUM}/global/networks/${NETWORK_NAME}`.
