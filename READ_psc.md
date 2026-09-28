## 

| Layer | Resource | Direction |
| :---- | :---- | :---- |
| **PSC endpoint for Google APIs** | Global address \+ forwarding rule \+ private DNS zone | Your VPC → Google APIs, privately |
| **PSC interface** | Subnet \+ network attachment \+ DNS peering | Agent Engine tenant → your VPC |

The interface gets the agent *into* your VPC. The endpoint \+ DNS is what it then uses to reach BigQuery privately. Build them in that order — the DNS zone must exist before Vertex peers to it.

---

## Step 0 — Set every variable once

Everything below derives from these. Run this block first in every new shell.

```sh
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
```

Confirm `PROJECT_ID` and `PROJECT_NUMBER` are non-empty before continuing.

---

## Step 1 — Enable the APIs

All of them, up front. The original runbook enabled `dns.googleapis.com` mid-stream, which fails the DNS commands if it wasn't already on.

```sh
gcloud services enable \
  compute.googleapis.com \
  dns.googleapis.com \
  aiplatform.googleapis.com \
  --project="${PROJECT_ID}"
```

---

## Step 2 — Create the Vertex AI service agent identity

**Must come before any IAM grant to that account.** The original granted roles to `service-<NUM>@gcp-sa-aiplatform...` before the identity existed, which fails with `Service account does not exist`.

```sh
gcloud beta services identity create \
  --service=aiplatform.googleapis.com \
  --project="${PROJECT_ID}"

# Confirm it resolves
gcloud iam service-accounts describe "${VERTEX_SA}" --project="${PROJECT_ID}" 2>/dev/null \
  || echo "NOTE: P4SA is Google-managed and may not be describable; proceed anyway."
```

---

## Step 3 — PSC endpoint for Google APIs

Internal entry point routing to Google APIs without traversing the public internet.

```sh
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
```

Verify:

```sh
gcloud compute forwarding-rules describe pscapis --global \
  --project="${PROJECT_ID}" --format="value(name,IPAddress,target)"
```

---

## Step 4 — Private DNS zone and records

Overrides public routing for Google APIs inside your VPC.

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

# All subdomains — catches bigquery.googleapis.com
# NOTE: the wildcard MUST be quoted or your shell expands it against local files
gcloud dns record-sets create "*.googleapis.com." \
  --zone="${DNS_ZONE_NAME}" \
  --type=A \
  --ttl=300 \
  --rrdatas="${PSC_IP}" \
  --project="${PROJECT_ID}"
```

Verify both records point at `${PSC_IP}`:

```sh
gcloud dns record-sets list --zone="${DNS_ZONE_NAME}" \
  --project="${PROJECT_ID}" --format="table(name,type,rrdatas)"
```

---

## Step 5 — PSC interface subnet and network attachment

The subnet must be in the **same region you deploy the agent to**.

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

> **IP range constraint:** Vertex cannot reach certain ranges from the PSC interface. Check the subnetwork IP range requirements in the PSC-I docs before picking something other than `10.0.1.0/28`.

Verify:

```sh
gcloud compute network-attachments describe "${NETWORK_ATTACHMENT_NAME}" \
  --region="${REGION}" --project="${PROJECT_ID}" \
  --format="value(name,connectionPreference,subnetworks[0])"
```

---

## Step 6 — Grant the Vertex AI service agent its roles

Now that the identity exists and the resources it must touch exist.

```sh
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
```

> `roles/dns.peer` must be granted **in the project that owns the DNS record**. Same project here, but if the zone lives elsewhere, grant it there instead.

> If `compute.networkAdmin` is too broad for your org, substitute a custom role carrying only `compute.networkAttachments.get` and `compute.networkAttachments.update`.

---

## Step 7 — Copy the agent folder

Run from the **repo base directory**. This creates the PSC copy without touching the original.

```sh
cd ~/agent-builder-labs-revised

cp -a data_analytics_agent/. data_analytics_agent_psc/
```

---

## Step 8 — Generate `.agent_engine_config.json`

Still in the base directory — **do not `cd` into the agent folder.** The original runbook `cd`'d in here and then never came back out, so the final `adk deploy ... data_analytics_agent_psc` resolved to a nonexistent nested path.

Resolve the VPC name back from the attachment, so the config is self-consistent with what you actually created:

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

echo "Resolved Project:    ${PROJECT_ID}"
echo "Resolved Region:     ${REGION}"
echo "Resolved Attachment: ${NETWORK_ATTACHMENT_NAME}"
echo "Resolved VPC:        ${RESOLVED_NETWORK}"
```

Write the config:

```sh
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
```

> `dns_peering_configs` is **not** optional in practice. Vertex does not auto-select a zone — omit it and DNS resolution silently falls back to public.

---

## Step 9 — Point the agent at its Cloud Run dependencies

```sh
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
```

Skip this step if your PSC variant doesn't call the catalog or judge.

---

## Step 10 — Deploy

From the base directory. `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION` were exported back in Step 0 — the original runbook referenced them here without ever setting them, so the deploy went out with empty `--project`/`--region`.

```sh
pwd   # must be the repo base, NOT inside ${AGENT_DIR}

adk deploy agent_engine \
  --project="${GOOGLE_CLOUD_PROJECT}" \
  --region="${GOOGLE_CLOUD_LOCATION}" \
  --display_name="${DISPLAY_NAME}" \
  --otel_to_cloud \
  .
```

---

&nbsp;

Playground query:

Confirm it egressed privately:

```sh
gcloud logging read \
  'resource.type="bigquery_project" AND protoPayload.serviceName="bigquery.googleapis.com"' \
  --project="${PROJECT_ID}" \
  --limit=5 \
  --format="yaml(timestamp, protoPayload.authenticationInfo.principalSubject, protoPayload.requestMetadata.callerIp, protoPayload.requestMetadata.callerNetwork, protoPayload.methodName)"
```

`callerIp` should be an internal RFC 1918 address from `${PSC_SUBNET_RANGE}`, and `callerNetwork` should name your VPC.

&nbsp;

&nbsp;

![][image1]

&nbsp;

[image1]: <data:image/png;base64,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>