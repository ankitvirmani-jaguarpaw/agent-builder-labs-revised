#!/usr/bin/env bash
set -euo pipefail

echo "==================================================================="
echo " Lab: Managed Agents API & Interactions API - Qwiklabs Setup"
echo "==================================================================="

export PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
export USER_EMAIL=$(gcloud config get-value account 2>/dev/null)
export BUCKET_REGION="${GOOGLE_CLOUD_REGION:-us-central1}"
export LOCATION="global"

if [ -z "$PROJECT_ID" ] || [ "$PROJECT_ID" = "(unset)" ]; then
    echo "ERROR: No active Google Cloud project set in gcloud."
    echo "Run 'gcloud config set project <your-qwiklabs-project-id>' first."
    exit 1
fi

if [ -z "$USER_EMAIL" ]; then
    echo "ERROR: No active Google account found. Run 'gcloud auth login' first."
    exit 1
fi

export PROJECT_NUM=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
export AGENT_SKILL_GCS_BUCKET="${AGENT_SKILL_GCS_BUCKET:-${PROJECT_ID}-managed-agent-skills}"

echo "Active Project:   ${PROJECT_ID} (Number: ${PROJECT_NUM})"
echo "Active Account:   ${USER_EMAIL}"
echo "Agent Location:   ${LOCATION}"
echo "Skill GCS Bucket: gs://${AGENT_SKILL_GCS_BUCKET}"

echo -e "\n=== 1. Enabling Required Google Cloud APIs ==="
gcloud services enable \
    aiplatform.googleapis.com \
    bigquery.googleapis.com \
    storage.googleapis.com \
    --project="${PROJECT_ID}"

echo "[OK] APIs enabled."

echo -e "\n=== 2. Configuring Student & Platform Service Agent IAM ==="
for role in "roles/aiplatform.user" "roles/bigquery.jobUser" "roles/bigquery.dataViewer"; do
    gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
        --member="user:${USER_EMAIL}" \
        --role="${role}" \
        --condition=None >/dev/null
done

gcloud beta services identity create --service=aiplatform.googleapis.com --project="${PROJECT_ID}" 2>/dev/null || true
SA_EMAIL="service-${PROJECT_NUM}@gcp-sa-aiplatform.iam.gserviceaccount.com"

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/aiplatform.serviceAgent" \
    --condition=None >/dev/null 2>&1 || true

echo "[OK] IAM roles verified for ${USER_EMAIL} and ${SA_EMAIL}."

echo -e "\n=== 3. Staging Bundled SKILL.md to Cloud Storage ==="
if ! gcloud storage buckets describe "gs://${AGENT_SKILL_GCS_BUCKET}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud storage buckets create "gs://${AGENT_SKILL_GCS_BUCKET}" \
        --project="${PROJECT_ID}" \
        --location="${BUCKET_REGION}" \
        --uniform-bucket-level-access
fi

# Managed Agents control plane requires storage.buckets.get + storage.objects.{create,delete,update} on the skill bucket
gcloud storage buckets add-iam-policy-binding "gs://${AGENT_SKILL_GCS_BUCKET}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/storage.admin" \
    --project="${PROJECT_ID}" >/dev/null

gcloud storage cp -r skills/data-analytics "gs://${AGENT_SKILL_GCS_BUCKET}/" >/dev/null
echo "[OK] Uploaded ./skills/data-analytics/SKILL.md -> gs://${AGENT_SKILL_GCS_BUCKET}/data-analytics/SKILL.md"

echo -e "\n=== 4. Writing Local .env & Installing Python Dependencies ==="
cat > .env <<EOF
GOOGLE_CLOUD_PROJECT=${PROJECT_ID}
GOOGLE_CLOUD_LOCATION=${LOCATION}
AGENT_SKILL_GCS_BUCKET=${AGENT_SKILL_GCS_BUCKET}
EOF

# Optional: if `knowledge-catalog-mcp` happens to be deployed in the same project, wire it automatically
RUN_SERVICE_URL=$(gcloud run services describe "knowledge-catalog-mcp" \
    --project="${PROJECT_ID}" \
    --region="${BUCKET_REGION}" \
    --format='value(status.url)' 2>/dev/null || true)
if [ -n "$RUN_SERVICE_URL" ]; then
    echo "CATALOG_MCP_URL=${RUN_SERVICE_URL}/sse" >> .env
    echo "[OK] Detected optional Cloud Run MCP server: ${RUN_SERVICE_URL}/sse"
fi

pip install --upgrade -r requirements.txt

echo -e "\n==================================================================="
echo " Setup Complete! Run the 4 Lab Steps in Order:"
echo "==================================================================="
echo "  Step 1 (Create Agent):            python step1_create_agent.py"
echo "  Step 2 (First Interaction):       python step2_first_interaction.py"
echo "  Step 3 (Follow-Up Interaction):   python step3_followup_interaction.py"
echo "  Step 4 (Reuse Existing Agent):    python step4_new_interaction.py"
echo ""
echo "  Cleanup when finished:            python cleanup_managed_agent.py"
echo "==================================================================="
