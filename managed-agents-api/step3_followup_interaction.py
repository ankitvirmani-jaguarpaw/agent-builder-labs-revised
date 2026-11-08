"""Step 3: Follow Up on the Previous Interaction (`previous_interaction_id` + `environment_id`).

Continues Session A from Step 2 on the same Managed Agent:
- Passes `previous_interaction_id` so the agent retains conversation memory
  (including the `bounce_rate` formula discussed in Step 2).
- Passes `environment_id` so `/tmp/bq_runner.py` built in Step 2 is still on disk.
- Injects a short-lived BigQuery OAuth header at the host egress proxy
  (`network.allowlist[].transform`) with `tools=[{"type": "code_execution"}]`
  so zero credentials ever enter sandbox memory or env vars.
"""

from __future__ import annotations

import argparse
import os

from dotenv import load_dotenv
from low_infra_harness import (
    LAB_DIR,
    ManagedTurnResult,
    build_bigquery_proxy_environment,
    create_genai_client,
    load_lab_state,
    mint_bigquery_access_token,
    resolve_project_id,
    run_managed_interaction,
    save_lab_state,
)


def run_step3(
    *,
    agent_id: str | None = None,
    previous_interaction_id: str | None = None,
    environment_id: str | None = None,
) -> ManagedTurnResult:
    """Follows up on Step 2's interaction and executes `/tmp/bq_runner.py` against BigQuery."""
    load_dotenv(LAB_DIR / ".env")

    project_id = resolve_project_id()
    state = load_lab_state()

    resolved_agent_id = (
        agent_id or os.getenv("MANAGED_AGENT_ID") or state.get("agent_id")
    )
    resolved_prev_id = previous_interaction_id or state.get("session_a_interaction_id")
    resolved_env_id = environment_id or state.get("session_a_environment_id")

    if not resolved_agent_id or not resolved_prev_id or not resolved_env_id:
        raise ValueError(
            "Missing Session A state handles. Run 'python step2_first_interaction.py' first."
        )

    bq_token = mint_bigquery_access_token()
    client = create_genai_client(project_id, location="global")

    print("=== Step 3: Follow Up on Previous Interaction ===")
    print(f"  agent_id                = {resolved_agent_id}")
    print(f"  previous_interaction_id = {resolved_prev_id}")
    print(f"  environment_id          = {resolved_env_id}")

    followup = run_managed_interaction(
        client,
        agent=resolved_agent_id,
        previous_interaction_id=resolved_prev_id,
        prompt=(
            "Following up on our previous turn: run your existing /tmp/bq_runner.py "
            "(or ./bq_runner.py) CLI in code_execution to validate and execute the partitioned "
            "query on `bigquery-public-data.google_analytics_sample.ga_sessions_*` for "
            "August 1, 2017 (`_TABLE_SUFFIX = '20170801'`), computing bounce_rate (using the "
            "formula from our previous turn) and the top 3 `trafficSource.source` values by "
            "visit count. If the outbound request returns HTTP 401 in the zero-credential "
            "sandbox, do NOT probe gcloud, /etc, or network interfaces—stop immediately and "
            "report the validated SQL query, dry-run gate status, and canonical metrics."
        ),
        environment=build_bigquery_proxy_environment(
            project_id,
            bq_access_token=bq_token,
            environment_id=resolved_env_id,
        ),
        tools=[{"type": "code_execution"}],
    )

    print("\n--- Agent Follow-Up Response ---")
    print(followup.output_text)

    save_lab_state(
        {"followup_interaction_id": followup.interaction_id},
        env_id=followup.environment_id or resolved_env_id,
    )

    print("\n[OK] Step 3 Complete — Chained Turn Executed:")
    print(f"  new interaction_id      = {followup.interaction_id}")
    print(f"  previous_interaction_id = {followup.previous_interaction_id}")
    print(f"  reused environment_id   = {followup.environment_id or resolved_env_id}")
    print("\nNext step: python step4_new_interaction.py")
    return followup


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Step 3: Follow up on Step 2 using previous_interaction_id + environment_id."
    )
    parser.add_argument("--agent-id", help="Override Managed Agent ID.")
    parser.add_argument("--prev-id", help="Override previous_interaction_id from Step 2.")
    parser.add_argument("--env-id", help="Override environment_id from Step 2.")
    args = parser.parse_args()
    run_step3(
        agent_id=args.agent_id,
        previous_interaction_id=args.prev_id,
        environment_id=args.env_id,
    )


if __name__ == "__main__":
    main()
