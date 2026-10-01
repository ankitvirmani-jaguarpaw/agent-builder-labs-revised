"""agent.py

Defines the Judge Agent with custom developer metrics for SQL governance and quality.
"""

from __future__ import annotations

import os
from google.adk.agents import LlmAgent

# Ensure Vertex AI backend is used
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true"
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "us-central1")

JUDGE_INSTRUCTION = """You are an expert Data Quality & SQL Governance Judge Agent.
Your responsibility is to evaluate analytical responses, SQL queries, and findings submitted by the Worker Agent.

You must evaluate each submission against these 4 custom developer metrics:
1. Query Efficiency (0-10):
   - Were partition filters (_TABLE_SUFFIX) used for date-sharded tables like ga_sessions_*?
   - Were costly SELECT * full-table scans avoided?
2. Semantic Alignment (0-10):
   - Does the calculation logic match the official Knowledge Catalog definitions?
3. Accuracy & Groundedness (0-10):
   - Are the numerical claims in the summary strictly supported by the SQL result set?
4. Preference Adherence (0-10):
   - Did the response respect user-requested date ranges, regions, or formatting?

OUTPUT FORMAT:
You MUST respond with valid, parseable JSON matching this exact structure:
{
  "scores": {
    "query_efficiency": <integer 0-10>,
    "semantic_alignment": <integer 0-10>,
    "accuracy": <integer 0-10>,
    "preference_adherence": <integer 0-10>
  },
  "passed": <true|false>,
  "critique": "<Actionable feedback explaining the scores and any violations>",
  "recommended_modifications": "<Concrete suggested query or answer revision, or null>"
}
"""

judge_agent = LlmAgent(
    name="judge_agent",
    model="gemini-2.5-pro",
    instruction=JUDGE_INSTRUCTION,
)
