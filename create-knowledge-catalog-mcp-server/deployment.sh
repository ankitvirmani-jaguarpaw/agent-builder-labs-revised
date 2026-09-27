#!/usr/bin/env bash
set -e

# =========================================================================
# Dynamic Parameter Initialization (No Hardcoding)
# =========================================================================

# 1. Dynamically fetch the current project ID
export PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
export REGION="${REGION:-us-central1}"
export SERVICE_NAME="${SERVICE_NAME:-knowledge-catalog-mcp}"

# Safety check: exit immediately if gcloud has no active project
if [ -z "$PROJECT_ID" ]; then
    echo "❌ ERROR: No active project found in environment or gcloud config."
    echo "Set your project using: gcloud config set project <PROJECT_ID>"
    exit 1
fi

# 2. Dynamically fetch the project number to construct the Compute SA
PROJECT_NUMBER=$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')
COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

echo "========================================================================="
echo " Deploying Knowledge Catalog MCP Server to Cloud Run"
echo " Project ID:     ${PROJECT_ID}"
echo " Project Number: ${PROJECT_NUMBER}"
echo " Service SA:     ${COMPUTE_SA}"
echo " Region:         ${REGION}"
echo " Service Name:   ${SERVICE_NAME}"
echo "========================================================================="

# =========================================================================
# Parameterized IAM Roles Configuration
# =========================================================================
REQUIRED_ROLES=(
    # Cloud Build & Artifact Registry roles
    "roles/storage.objectViewer"
    "roles/logging.logWriter"
    "roles/artifactregistry.writer"
    "roles/cloudbuild.builds.builder"
    
    # Dataplex Knowledge Catalog roles
    "roles/dataplex.catalogViewer"
    "roles/dataplex.viewer"
    "roles/dataplex.metadataReader"
)

echo "Ensuring required IAM bindings on ${COMPUTE_SA}..."
for ROLE in "${REQUIRED_ROLES[@]}"; do
    gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
        --member="serviceAccount:${COMPUTE_SA}" \
        --role="${ROLE}" \
        --condition=None \
        --quiet > /dev/null
done
echo "✅ IAM permissions validated successfully."

# =========================================================================
# Source Build & Cloud Run Deployment
# =========================================================================
echo "Building container from source and deploying to Cloud Run..."
gcloud run deploy "${SERVICE_NAME}" \
    --source . \
    --platform managed \
    --region "${REGION}" \
    --set-env-vars GOOGLE_CLOUD_PROJECT="${PROJECT_ID}",GOOGLE_CLOUD_REGION="${REGION}" \
    --allow-unauthenticated

# =========================================================================
# Output Deployment Information
# =========================================================================
SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
    --platform managed \
    --region "${REGION}" \
    --project "${PROJECT_ID}" \
    --format 'value(status.url)')

echo -e "\n========================================================================="
echo " 🎉 Knowledge Catalog MCP Server Deployed Successfully!"
echo " Live SSE Endpoint: ${SERVICE_URL}/sse"
echo " Export this for your Agent: export CATALOG_MCP_URL=\"${SERVICE_URL}/sse\""
echo "========================================================================="
