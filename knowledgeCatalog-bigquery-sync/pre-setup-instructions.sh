#!/usr/bin/env bash
set -e

echo "=== 1. Detecting Project and Account ==="
export PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
export USER_EMAIL=$(gcloud config get-value account 2>/dev/null)

if [ -z "$PROJECT_ID" ]; then
    echo "❌ ERROR: No active project set. Run 'gcloud config set project <project-id>' first."
    exit 1
fi

if [ -z "$USER_EMAIL" ]; then
    echo "❌ ERROR: No active user account found. Run 'gcloud auth login' first."
    exit 1
fi

echo "Project ID: ${PROJECT_ID}"
echo "User Email: ${USER_EMAIL}"

echo -e "\n=== 2. Enabling Required GCP APIs ==="
gcloud services enable \
    serviceusage.googleapis.com \
    dataplex.googleapis.com \
    datacatalog.googleapis.com \
    bigquery.googleapis.com

echo -e "\n=== 3. Binding Required IAM Roles ==="
ROLES=(
    "roles/serviceusage.serviceUsageConsumer"
    "roles/dataplex.admin"
    "roles/bigquery.dataViewer"
    "roles/bigquery.jobUser"
)

for role in "${ROLES[@]}"; do
    echo "Assigning ${role}..."
    gcloud projects add-iam-policy-binding "$PROJECT_ID" \
        --member="user:${USER_EMAIL}" \
        --role="$role" \
        --condition=None
done

echo -e "\n=== 4. Installing Python Client Libraries ==="
pip install --upgrade google-cloud-bigquery google-cloud-dataplex

echo -e "\n[✓] Prerequisites setup completed successfully!"