"""agent.py

Defines the Judge Agent with custom developer metrics for SQL governance and quality.
"""

from __future__ import annotations

import os

from google.adk.agents import LlmAgent
from google.adk.apps import App

# Ensure Vertex AI backend is used
LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "us")
MODEL_NAME = os.getenv("MODEL_NAME", "gemini-3.8-flash")
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true"
os.environ["GOOGLE_CLOUD_LOCATION"] = LOCATION

JUDGE_INSTRUCTION = """You are an elite BigQuery & SQL Governance Evaluation Agent.
Your job is to strictly evaluate SQL queries proposed or executed by other agents.

Score each query on:
1. Syntax correctness
2. Partition pruning discipline (reject full table scans on large tables)
3. Column selection discipline (reject SELECT * queries)
4. Knowledge Catalog semantic alignment

Output a JSON summary:
{
  "evaluation_status": "PASS" | "FAIL",
  "confidence_score": float,
  "metrics": {
    "syntax_valid": bool,
    "partition_enforced": bool,
    "no_select_star": bool,
    "catalog_aligned": bool
  },
  "feedback": "string explanation"
}
"""

judge_agent = LlmAgent(
    name="judge_agent",
    model=MODEL_NAME,
    instruction=JUDGE_INSTRUCTION,
)

app = App(
    name="judge_agent",
    root_agent=judge_agent,
)
