# Deploy the Judge Agent and Data Analytics Worker Agent

Continuation of the build guide. Deploys the Judge Agent to Cloud Run over A2A,
then the Data Analytics Worker Agent to Vertex AI Agent Engine.

## Prerequisites

Completed from the build stage:

- [x] Knowledge Catalog in Dataplex (provisioned & tested)
- [x] Knowledge Catalog MCP Server on Cloud Run (live & tested)
- [x] Data Analytics Agent source code created and unit tested
- [ ] Judge Agent on Cloud Run via A2A
- [ ] Data Analytics Worker Agent on Vertex AI Agent Engine

---

## Stage 4: Deploy the Judge Agent to Cloud Run

```bash
cd ..
cd judge-agent
gcloud services enable aiplatform.googleapis.com

chmod +x deploy-judge-agent-cloud-run.sh
./deploy-judge-agent-cloud-run.sh
```

Once the script finishes, capture the service URL and set it back on the service
so the agent card advertises its own public address:

```bash
export JUDGE_AGENT_URL="$(gcloud run services describe judge-agent \
  --region us-central1 --format 'value(status.url)')"

gcloud run services update judge-agent \
  --region us-central1 \
  --set-env-vars A2A_PUBLIC_URL="${JUDGE_AGENT_URL}"
```

Verify the Judge Agent publishes its A2A agent card:

```bash
curl -s "${JUDGE_AGENT_URL}/.well-known/agent-card.json" | jq .
```

You will see the agent card JSON describing the Judge Agent's capabilities,
skills, and endpoints over the A2A protocol.

### Grant Vertex AI access to the Cloud Run service account

```bash
PROJECT_ID=$(gcloud config get-value project)
PROJECT_NUM=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${PROJECT_NUM}-compute@developer.gserviceaccount.com" \
  --role="roles/aiplatform.user"
```

### Test the deployed Judge Agent

```bash
curl -s -X POST "${JUDGE_AGENT_URL}/" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "method": "message/send",
    "params": {
      "message": {
        "messageId": "msg-002",
        "role": "user",
        "parts": [
          {
            "kind": "text",
            "text": "Please evaluate this Worker Agent action:\nUser Question: How many visits did we get?\nGenerated SQL: SELECT SUM(totals.visits) FROM `bigquery-public-data.google_analytics_sample.ga_sessions_*`;\nAnswer: We had 500,000 visits."
          }
        ]
      }
    },
    "id": 2
  }' | jq .
```

You should get an evaluation response from the Judge Agent running on Cloud Run.

### Progress

- [x] Knowledge Catalog in Dataplex (provisioned & tested)
- [x] Knowledge Catalog MCP Server on Cloud Run (live & tested)
- [x] Data Analytics Agent created
- [x] Judge Agent on Cloud Run via A2A (live & tested)
- [ ] Data Analytics Worker Agent on Vertex AI Agent Engine

---

## Stage 5: Connect the Worker Agent to the live Judge Agent

Update `agent.py` to include the Judge Agent via A2A, then test locally.

```bash
cd ..
cd data_analytics_agent
```

Set the environment before testing:

```bash
# Dynamically retrieve the deployed Judge Agent URL from Cloud Run
export JUDGE_AGENT_URL="$(gcloud run services describe judge-agent \
  --region us-central1 --format 'value(status.url)')"
export GOOGLE_CLOUD_PROJECT=$(gcloud config get-value project)
export GOOGLE_GENAI_USE_VERTEXAI="true"
export GOOGLE_CLOUD_LOCATION="us-central1"
export CATALOG_MCP_URL="$(gcloud run services describe knowledge-catalog-mcp \
  --region us-central1 --format='value(status.url)')/sse"

echo "Judge Agent URL: ${JUDGE_AGENT_URL}"
```

Run the test:

```bash
python3 test_agent.py
```

Focus on how the Worker Agent calls the Judge Agent over A2A.

---

## Stage 6: Deploy the Worker Agent to Vertex AI Agent Engine

> Run everything below from the **base directory**, not from inside
> `data_analytics_agent`.

### 1. Set the environment

```bash
export GOOGLE_CLOUD_PROJECT=$(gcloud config get-value project)
export GOOGLE_CLOUD_LOCATION="us-central1"
export GOOGLE_GENAI_USE_VERTEXAI="true"

export PROJECT_ID="${GOOGLE_CLOUD_PROJECT}"
export REGION="${GOOGLE_CLOUD_LOCATION}"
export PROJECT_NUM=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")
```

### 2. Write the agent's `.env` from the live Cloud Run URLs

```bash
CATALOG_BASE_URL=$(gcloud run services describe knowledge-catalog-mcp \
   --project="${PROJECT_ID}" \
   --region="${REGION}" \
   --format='value(status.url)')

JUDGE_BASE_URL=$(gcloud run services describe judge-agent \
   --project="${PROJECT_ID}" \
   --region="${REGION}" \
   --format='value(status.url)')

cat <<EOF > data_analytics_agent/.env
GOOGLE_CLOUD_PROJECT=${PROJECT_ID}
GOOGLE_CLOUD_LOCATION=${REGION}
CATALOG_MCP_URL=${CATALOG_BASE_URL}/sse
JUDGE_AGENT_URL=${JUDGE_BASE_URL}
EOF

echo "Generated .env contents:"
cat data_analytics_agent/.env
```

### 3. Grant BigQuery job permissions

For the Compute Engine default service account:

```bash
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${PROJECT_NUM}-compute@developer.gserviceaccount.com" \
  --role="roles/bigquery.jobUser"
```

If testing locally with your authenticated user account via ADC:

```bash
USER_EMAIL=$(gcloud config get-value account)
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="user:${USER_EMAIL}" \
  --role="roles/bigquery.jobUser"
```

### 4. Deploy

```bash
adk deploy agent_engine \
 --project="${GOOGLE_CLOUD_PROJECT}" \
 --region="${GOOGLE_CLOUD_LOCATION}" \
 --display_name="data_analytics_nogateway_nopsc" \
 --otel_to_cloud \
 data_analytics_agent
```

Fetch the agent ID from the deployment output, e.g.
`projects/300803947057/locations/us-central1/reasoningEngines/4841997970318557184`.

```bash
gcloud services enable apphub.googleapis.com --project="$(gcloud config get-value project)"
```

---

## Stage 7: Test the deployed Worker Agent

### Resolve the endpoint dynamically

```bash
export GOOGLE_CLOUD_PROJECT=$(gcloud config get-value project)
export GOOGLE_CLOUD_LOCATION="us-central1"
DISPLAY_NAME="data_analytics_nogateway_nopsc"

# Resolve project number
PROJECT_NUM=$(gcloud projects describe "$GOOGLE_CLOUD_PROJECT" --format="value(projectNumber)")

# Resolve the Reasoning Engine ID from the display name
TOKEN=$(gcloud auth print-access-token)
REASONING_ENGINE_ID=$(curl -s \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  "https://${GOOGLE_CLOUD_LOCATION}-aiplatform.googleapis.com/v1/projects/${PROJECT_NUM}/locations/${GOOGLE_CLOUD_LOCATION}/reasoningEngines" \
  | jq -r --arg NAME "$DISPLAY_NAME" '.reasoningEngines[] | select(.displayName == $NAME) | .name | split("/")[-1]' \
  | head -n 1)

echo "Resolved Project Number:      $PROJECT_NUM"
echo "Resolved Reasoning Engine ID: $REASONING_ENGINE_ID"

BASE_ENDPOINT="https://${GOOGLE_CLOUD_LOCATION}-aiplatform.googleapis.com/v1/projects/${PROJECT_NUM}/locations/${GOOGLE_CLOUD_LOCATION}/reasoningEngines/${REASONING_ENGINE_ID}"
```

### Create a session

```bash
SESSION_RESP=$(curl -s -X POST \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" \
  "${BASE_ENDPOINT}:query" \
  -d '{
    "class_method": "async_create_session",
    "input": {
      "user_id": "analyst_1"
    }
  }')

echo "Raw Response: ${SESSION_RESP}"

SESSION_ID=$(echo "$SESSION_RESP" | jq -r '.output.id')
echo "Created Session ID: ${SESSION_ID}"
```

### Send a query

```bash
curl -X POST \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" \
  "${BASE_ENDPOINT}:streamQuery?alt=sse" \
  -d "{
    \"class_method\": \"async_stream_query\",
    \"input\": {
      \"user_id\": \"analyst_1\",
      \"session_id\": \"${SESSION_ID}\",
      \"message\": \"What datasets are available?\"
    }
  }"
```

### Continue the conversation

Because this is a multi-turn conversation in the same `SESSION_ID`, follow-up
turns reuse the same session:

```bash
curl -X POST \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" \
  "${BASE_ENDPOINT}:streamQuery?alt=sse" \
  -d "{
    \"class_method\": \"async_stream_query\",
    \"input\": {
      \"user_id\": \"analyst_1\",
      \"session_id\": \"${SESSION_ID}\",
      \"message\": \"Please list datasets in this project\"
    }
  }"
```

Enable the Agent Registry API:

```bash
gcloud services enable agentregistry.googleapis.com
```

---

## Stage 8: Query the catalog and the data

### Catalog-only questions (no BigQuery scan)

Ask the agent directly:

- "What are the details, table names, and schema for `google-analytics-catalog_entry`
  from the knowledge catalog?"
- "What metrics and formulas are defined in the knowledge catalog for Google
  Analytics?"

### Grant the agent identity access to BigQuery

```bash
PROJECT_ID=$(gcloud config get-value project)
PROJECT_NUM=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")

# Vertex AI Reasoning Engine service agent
RE_SERVICE_ACCOUNT="serviceAccount:service-${PROJECT_NUM}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"

echo "Binding roles for project: ${PROJECT_ID} (${PROJECT_NUM})"
echo "Target service account: ${RE_SERVICE_ACCOUNT}"

# Solves bigquery.jobs.create
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="$RE_SERVICE_ACCOUNT" \
  --role="roles/bigquery.jobUser"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="$RE_SERVICE_ACCOUNT" \
  --role="roles/bigquery.dataViewer"
```

### Ask data questions in the Playground

Open your agent in the Agent Engine console and choose **Playground**:

- "Using the google analytics catalog entry, what is the bounce rate for last week?"
- "Show me the top traffic sources from the Google Analytics sample dataset."
- "can you remember my favorite metric is bounce rate"?


What happens, given the system prompt rules:

1. The agent reads the table schema and partition requirements (`_TABLE_SUFFIX`)
   from `google-analytics-catalog_entry`.
2. It generates SQL against the external/public dataset while billing query
   execution to your own project.
3. It hands off to `judge_agent` for review before returning the result.

---

## Troubleshooting: BigQuery access errors

```bash
gcloud auth application-default print-access-token
```

Grant the roles to both your user and the Compute Engine default service account:

```bash
PROJECT_ID=$(gcloud config get-value project)
PROJECT_NUM=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")
CURRENT_USER=$(gcloud config get-value account)

# 1. Your current user login
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="user:${CURRENT_USER}" \
  --role="roles/bigquery.jobUser"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="user:${CURRENT_USER}" \
  --role="roles/bigquery.dataViewer"

# 2. Compute Engine / Cloud Shell default service account
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${PROJECT_NUM}-compute@developer.gserviceaccount.com" \
  --role="roles/bigquery.jobUser"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${PROJECT_NUM}-compute@developer.gserviceaccount.com" \
  --role="roles/bigquery.dataViewer"
```

Re-authenticate ADC and verify with a direct query:

```bash
gcloud auth application-default login

bq query --use_legacy_sql=false \
  --project_id="$(gcloud config get-value project)" \
  "SELECT SUM(IFNULL(totals.bounces, 0)) / COUNT(totals.visits) AS bounce_rate
   FROM \`bigquery-public-data.google_analytics_sample.ga_sessions_*\`
   WHERE _TABLE_SUFFIX BETWEEN '20170725' AND '20170801'"
```
