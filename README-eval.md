# Evaluation Gate for the Data Analytics Agent

Runs Vertex AI Gen AI Evaluation against the deployed agent's traces and applies
a hybrid quality + tokenomics gate.

## Prerequisites

- Data Analytics Worker Agent deployed to Vertex AI Agent Engine
- Judge Agent live on Cloud Run
- Knowledge Catalog MCP Server live on Cloud Run

---

## Step 1: Select traces in the Evaluation tab

In the Agent Engine console, open the first agent you deployed and go to the
**Evaluation** tab. Select all the traces you want to evaluate.

*(Screenshot placeholder.)*

## Step 2: Create the output GCS bucket

Create the Cloud Storage bucket that will hold the evaluation results.

*(Screenshot placeholder.)*

## Step 3: Select the metrics

Pick the metrics for the run. You can also define custom metrics here.

*(Screenshot placeholder.)*

## Step 4: Run the evaluation gate

```bash
cd eval_gate
python3 gate_eval.py --scenario=pass
python3 gate_eval.py --scenario=fail
```

*(Screenshot placeholder.)*

---

## What is happening behind the scenes

### Vertex AI backend API call

When `eval_task.evaluate()` is called, the Vertex AI SDK sends the `eval_df`
DataFrame over Google Cloud APIs to the Vertex AI Evaluation backend in your
project (`GOOGLE_CLOUD_PROJECT`) and region (`us-central1`).

### Automated LLM-as-a-judge evaluation

Vertex AI invokes internal judge models using official Google rubric templates:

| Metric | What it grades |
|---|---|
| `QUESTION_ANSWERING_QUALITY` | How well the agent's response answers the user's prompt, compared against the reference ground truth. |
| `GROUNDEDNESS` | Whether the agent hallucinated or stuck strictly to the ground-truth reference material. |

### Production hybrid gating

The code computes a composite score:

- **70% — Vertex AI Gen AI Evaluation Service metrics** (QA Quality + Groundedness)
- **30% — Tokenomics governance**, verifying that the agent:
  - invoked `query_knowledge_catalog`,
  - called the `judge_agent`, and
  - never ran unpartitioned queries.
