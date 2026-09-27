# Build Knowledge Catalog, Knowledge Catalog MCP Server and Agent Source Code

**(No Agent Deployment)** — Python + ADK + Shell Scripts

## Objective

Understand the software development behind agents using ADK, Agent-to-Agent (A2A)
interaction, Agent-to-MCP interaction, and unit testing.

> Deployment of this system is covered in the next section: **Scale**.

The system mixes several component types: a worker agent doing the main work, a
judge agent, and MCP servers.

## Components

| Component | Role |
|---|---|
| **Worker Agent / Data Analytics Agent** | Interacts with the user, queries real data from BigQuery public datasets, and remembers preferences across sessions. |
| **Judge Agent** | Judges the quality of the Worker Agent's responses. Contains the custom metrics developers define. |
| **BigQuery MCP Server** | GCP-managed BigQuery MCP server the agent connects to for data access. |
| **Knowledge Catalog MCP Server** | Custom MCP server the agent consults *before* reaching out to BigQuery, for metadata and the semantic layer needed to optimize queries. Also helps tokenomics: semantic questions are answered from the MCP server instead of a BigQuery scan. |

---

## Stage 1: Creation of MCP Source Code

### Step 1: Create the Knowledge Catalog

Not the MCP server itself — this syncs BigQuery into the Knowledge Catalog so a
semantic layer exists there.

```bash
git clone https://github.com/ankitvirmani-jaguarpaw/agent-builder-labs-revised.git
cd agent-builder-labs-revised
gcloud auth application-default login
```

Re-point gcloud and your environment to the project:

```bash
export GOOGLE_CLOUD_PROJECT=$(gcloud projects list --format="value(projectId)" --limit=1)
gcloud config set project "$GOOGLE_CLOUD_PROJECT"
```

Test that credentials return a valid token:

```bash
echo "GOOGLE_CLOUD_PROJECT is set to: $GOOGLE_CLOUD_PROJECT"
gcloud auth application-default print-access-token
```

Run the sync:

```bash
cd knowledgeCatalog-bigquery-sync

# Make both shell scripts executable
chmod +x pre-setup-instructions.sh test-instructions.sh

# Run the setup script and the Python setup
./pre-setup-instructions.sh
python3 knowledge-catalog-setup.py
```

*(Expected output: screenshot placeholder.)*

### Step 2: Create the Knowledge Catalog MCP Server

```bash
cd ../create-knowledge-catalog-mcp-server
chmod +x instructions.sh
./instructions.sh
python3 catalog-mcp-server.py
```

**Second terminal** — check the SSE handshake:

```bash
curl -i -N -H "Accept: text/event-stream" http://localhost:8080/sse
```

**Third terminal** — pre-test setup:

```bash
cd agent-builder-labs-revised/create-knowledge-catalog-mcp-server
chmod +x instructions-pre-test-script.sh
./instructions-pre-test-script.sh
```

Then run the test client:

```bash
cd agent-builder-labs-revised/create-knowledge-catalog-mcp-server
python3 test-knowledge-catalog-mcp-code.py
```

This script acts as an MCP client: it connects to the running MCP server at
`http://localhost:8080/sse`, initializes the session, checks for all available
tools (including semantic search), and calls each tool to confirm data is
retrieved from Dataplex.

> At this point the MCP server code is tested only. It is not yet deployed to
> Cloud Run.

---

## Stage 2: Deploy the MCP Server to Cloud Run

```bash
gcloud auth login

cd create-knowledge-catalog-mcp-server/
chmod +x deployment-instructions.sh
./deployment-instructions.sh

chmod +x deployment.sh
./deployment.sh
```

Capture your Cloud Run URL:

```bash
export CATALOG_MCP_URL="$(gcloud run services describe knowledge-catalog-mcp \
  --region us-central1 --format='value(status.url)')/sse"
```

> **Note:** Replace this with the Cloud Run URL output from the previous step if
> the service name or region differs.

Test the deployed server:

```bash
python3 test-mcp-server-cloud-run.py
```

This exercises every tool the deployed MCP server exposes, including semantic
search.

### How the semantic search works

- **Embedding & intent matching** — with `semantic_search=True`, Dataplex runs an
  internal semantic embedding model on the query text instead of strict regex or
  boolean string matching.
- **Concept mapping** — query `"unique active people over past month"` matched the
  catalog entries `monthly-active-users` and `rolling 30-day window`. Neither
  "people" nor "month" appear in the stored entry; the model mapped
  people → users/visitors and past month → 30-day window.
- **Partition spec mapping** — query `"partitioned raw session logs and tables"`
  mapped directly to `ga-sessions-table-spec` on intent rather than wording.

Semantic search here is powered entirely by Dataplex's built-in vector and intent
search engine.

**MILESTONE 1 complete: MCP server built and deployed on Cloud Run.**

---

## Stage 3: Build the Agent Code (build and test only, no deployment)

Two agents:

1. **Data Analytics Agent** — queries backend data, using the Knowledge Catalog
   as the semantic layer.
2. **Judge Agent** — scores the worker's responses.

### Tokenomics governance in the Data Analytics Agent

- **System prompt guardrail (prompt-level routing)** — explicit negative
  constraints instruct the model to stop and return the catalog definition
  immediately when the user's intent is informational, conceptual, or
  schema-oriented.
- **Deterministic code pre-flight check (code-level circuit breaker)** — an
  automated check inspects the Knowledge Catalog response before deciding whether
  BigQuery is needed. Queries asking only for formulas, definitions, schema
  columns, or metric rules are marked complete and bypass BigQuery entirely.

### Build and test

```bash
cd ..
cd data_analytics_agent
nano instructions.sh
```

Replace the MCP server URL in `instructions.sh` with the Cloud Run URL you
deployed in Stage 2.

```bash
gcloud auth application-default login
gcloud services enable aiplatform.googleapis.com
python3 test_agent.py
```

### Expected dry-run results

**Strict tokenomics (Tests 1 & 2)**

- Asked for the bounce rate formula: the agent recognized the conceptual request,
  queried the Knowledge Catalog MCP server, and returned the formula plus the
  partition warning straight from the catalog. Zero BigQuery scans.
- Asked for partitioning rules: the agent queried the catalog, identified
  `_TABLE_SUFFIX` in `YYYYMMDD` format, and answered without touching BigQuery.

**Optimized BigQuery execution (Test 3)**

- Asked for concrete traffic counts on August 1, 2017: the agent applied the
  partition rule from the catalog (`_TABLE_SUFFIX = '20170801'`), executed against
  BigQuery, and returned `(direct)` 2,166, `youtube.com` 180,
  `analytics.google.com` 57.

**Memory Bank resilience**

- `Failed to preload memory` warnings confirm the local fallback works:
  `GOOGLE_CLOUD_AGENT_ENGINE_ID` is absent during local testing, so the agent
  bypasses Agent Engine Memory Bank without interrupting execution.

### Why the Data Analytics Agent is not deployed yet

The Judge Agent must be deployed to Cloud Run first.

```
[Knowledge Catalog MCP]   (Cloud Run)      <-- DEPLOYED & LIVE ✅
          ▲
          │
[Judge Agent (A2A)]       (Cloud Run)      <-- MUST DEPLOY NEXT ⏳
          ▲
          │
[Data Analytics Worker]   (Agent Engine)   <-- DEPLOY LAST ⏹️
```

The worker calls the Judge Agent over the network using the A2A protocol:

```python
judge_proxy = RemoteA2aAgent(
    name="judge_agent",
    agent_card=f"{JUDGE_AGENT_URL}/.well-known/agent-card.json",
    use_legacy=False,
)
```

For `RemoteA2aAgent` to initialize, `JUDGE_AGENT_URL` must already exist and be
serving its A2A agent card. Deploying the worker to Agent Engine now would leave
it without a live Judge Agent URL.

### Step 4: Build and unit test the Judge Agent (no deployment)

```bash
cd ..
cd judge-agent
python test_judge_agent.py
```
