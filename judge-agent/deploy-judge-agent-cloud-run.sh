#!/usr/bin/env bash
set -e

export PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
export REGION="${REGION:-us-central1}"
export GOOGLE_CLOUD_LOCATION="${GOOGLE_CLOUD_LOCATION:-us}"
export MODEL_NAME="${MODEL_NAME:-gemini-3.8-flash}"
export SERVICE_NAME="judge-agent"

if [ -z "$PROJECT_ID" ]; then
    echo "❌ ERROR: No active project found. Run 'gcloud config set project <your-project>' first."
    exit 1
fi

echo "========================================================================="
echo " Deploying Judge Agent to Cloud Run (A2A Ready)"
echo " Project:     ${PROJECT_ID}"
echo " Region:      ${REGION}"
echo " Model:       ${MODEL_NAME} (${GOOGLE_CLOUD_LOCATION})"
echo " Service:     ${SERVICE_NAME}"
echo "========================================================================="

# 1. Pre-fetch service URL for A2A self-advertisement
SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" --platform managed --region "${REGION}" --format 'value(status.url)' 2>/dev/null || echo "")

# 2. Deploy in a single step with all variables preserved
gcloud run deploy "${SERVICE_NAME}" \
    --source . \
    --platform managed \
    --region "${REGION}" \
    --set-env-vars GOOGLE_CLOUD_PROJECT="${PROJECT_ID}",GOOGLE_GENAI_USE_VERTEXAI="true",GOOGLE_CLOUD_LOCATION="${GOOGLE_CLOUD_LOCATION:-us}",MODEL_NAME="${MODEL_NAME:-gemini-3.8-flash}",A2A_PUBLIC_URL="${SERVICE_URL}" \
    --allow-unauthenticated

# 3. Confirm and print live URL
LIVE_URL=$(gcloud run services describe "${SERVICE_NAME}" --platform managed --region "${REGION}" --format 'value(status.url)')

echo -e "\n========================================================================="
echo " 🎉 Judge Agent Deployed Successfully via A2A!"
echo " Base URL:       ${LIVE_URL}"
echo " A2A Agent Card: ${LIVE_URL}/.well-known/agent-card.json"
echo " Export this:    export JUDGE_AGENT_URL=\"${LIVE_URL}\""
echo "========================================================================="
