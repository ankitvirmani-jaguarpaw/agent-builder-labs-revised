---
name: data-analytics
description: Self-contained BigQuery data analytics skill defining canonical Google Analytics semantic metrics, _TABLE_SUFFIX partition rules, and a zero-secret sandboxed Python BigQuery REST runner.
---

# Data Analytics & SQL Governance Skill

Follow this workflow whenever answering metric definitions, schema questions, or live BigQuery analytics queries on `bigquery-public-data.google_analytics_sample.ga_sessions_*`.

## 1. Canonical Semantic Definitions & Zero-Scan Rule

For conceptual, metric formula, or partition metadata questions, answer **strictly from the definitions below** (and cross-check `knowledge-catalog-mcp` tools only if attached on the agent). **Never run a BigQuery scan just to look up a metric formula or partition rule:**

- **Dataset & Table Pattern:** `bigquery-public-data.google_analytics_sample.ga_sessions_*`
  - **Partition Key:** `_TABLE_SUFFIX` (string format `YYYYMMDD`, e.g. `_TABLE_SUFFIX = '20170801'`).
  - **Optimization Rule:** Always filter `WHERE _TABLE_SUFFIX = 'YYYYMMDD'` (or `BETWEEN '...' AND '...'`) to avoid full wildcard scans.
- **Official Metric — `bounce_rate` (Bounce Rate):**
  - **Definition:** Percentage of single-interaction visits where the visitor leaves from the entrance page without further actions.
  - **Canonical SQL Formula:** `SUM(IFNULL(totals.bounces, 0)) / COUNT(totals.visits)` (equivalently `COUNTIF(totals.bounces = 1) / COUNT(totals.visits)`).
- **Official Metric — `monthly_active_users` (Monthly Active Users / MAU):**
  - **Definition:** Count of distinct active visitors across a rolling 30-day `_TABLE_SUFFIX` window.
  - **Canonical SQL Formula:** `COUNT(DISTINCT fullVisitorId)` filtered by a 30-day `_TABLE_SUFFIX` range.

<!-- PROTECTED:START -->
## 2. Non-Negotiable Sandbox & SQL Governance Guardrails (Immutable)

When live row values or aggregations are requested, build and execute `/tmp/bq_runner.py` (and copy a mirror to `./bq_runner.py` in the workspace root) inside `code_execution` using Python's standard library (`urllib.request`, `json`, `os`, `re`, `shutil`, `sys`):

1. **Zero Ambient Secrets in Code:**
   - Read the billing project from `os.environ.get("GOOGLE_CLOUD_PROJECT", "qwiklabs-gcp")`.
   - Send HTTPS `POST` requests directly to `https://bigquery.googleapis.com/bigquery/v2/projects/{project_id}/queries` (`Content-Type: application/json`).
   - Do **not** probe `gcloud`, `/etc`, `iptables`, metadata servers, or local ports inside the sandbox—if an unauthenticated sandbox returns `HTTP 401`, catch `HTTPError` cleanly in `/tmp/bq_runner.py` without retrying or scanning the filesystem.
2. **Local Pre-Flight SQL Guardrails (Before Any Network Call):**
   - Reject any query matching `SELECT\s+\*` (case-insensitive).
   - If the query references `ga_sessions`, reject it unless `_TABLE_SUFFIX` is present in the SQL text.
3. **Mandatory Dry-Run Byte Gate (`dryRun: true`):**
   - First `POST` `{"query": sql, "useLegacySql": false, "dryRun": true}`.
   - Parse `totalBytesProcessed` from the JSON response and abort if `int(totalBytesProcessed) > 100_000_000` (`100 MB`).
4. **Partition-Safe Execution (`dryRun: false`):**
   - `POST` `{"query": sql, "useLegacySql": false, "maximumBytesBilled": "100000000"}` (filtering `_TABLE_SUFFIX = '20170801'` for August 1, 2017).
   - Zip `schema.fields` names with each row's `f[].v` values and print structured JSON including `dry_run_bytes` and `rows`.
<!-- PROTECTED:END -->

## 3. Grounded Reporting

- Cite the `_TABLE_SUFFIX` partition filter, `dry_run_bytes` processed, and canonical metric formula used.
- Every numerical figure in the final response must match the `/tmp/bq_runner.py` output verbatim.
