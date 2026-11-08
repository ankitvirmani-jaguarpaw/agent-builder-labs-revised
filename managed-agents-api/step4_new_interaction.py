"""Step 4: Create a New Interaction with the Existing Agent (`agent=agent_id`).

Reuses the exact same Managed Agent created in Step 1 for a brand-new session
(Session B), explicitly OMITTING `previous_interaction_id` and `environment_id`:
- No re-provisioning (`client.agents.create` is not called again)—the agent's
  `system_instruction`, mounted `SKILL.md`, and tool definitions are reused immediately.
- Conversation history is completely fresh (`previous_interaction_id=None`).
- Remote Linux sandbox is newly provisioned and isolated (`new_env_id != step2_env_id`),
  so Session A's `/tmp/bq_runner.py` is absent in this clean workspace.
"""

from __future__ import annotations

import argparse
import os

from dotenv import load_dotenv
from low_infra_harness import (
    LAB_DIR,
    ManagedTurnResult,
    create_genai_client,
    load_lab_state,
    resolve_project_id,
    run_managed_interaction,
    save_lab_state,
)


def run_step4(agent_id: str | None = None) -> ManagedTurnResult:
    """Starts a brand-new isolated interaction against the existing `agent_id`."""
    load_dotenv(LAB_DIR / ".env")

    project_id = resolve_project_id()
    state = load_lab_state()
    resolved_agent_id = (
        agent_id or os.getenv("MANAGED_AGENT_ID") or state.get("agent_id")
    )
    if not resolved_agent_id:
        raise ValueError(
            "No Managed Agent ID found. Run 'python step1_create_agent.py' first."
        )

    session_a_env_id = state.get("session_a_environment_id")
    client = create_genai_client(project_id, location="global")

    print("=== Step 4: New Independent Interaction on Existing Agent ===")
    print(f"  reused agent_id         = {resolved_agent_id}")
    print("  previous_interaction_id = None (fresh conversation)")
    print("  environment_id          = None (provisions clean isolated sandbox)")

    session_b = run_managed_interaction(
        client,
        agent=resolved_agent_id,
        prompt=(
            "In this brand-new session: (1) state our official SQL formula for "
            "monthly_active_users from your mounted skill in ./skills/ without running "
            "BigQuery SQL, and (2) check via code_execution whether `/tmp/bq_runner.py` exists "
            "in this fresh sandbox."
        ),
    )

    print("\n--- Agent Fresh-Session Response ---")
    print(session_b.output_text)

    save_lab_state(
        {
            "session_b_interaction_id": session_b.interaction_id,
            "session_b_environment_id": session_b.environment_id,
        },
        env_id=session_b.environment_id,
    )

    isolated = (
        session_b.environment_id != session_a_env_id
        if (session_a_env_id and session_b.environment_id)
        else True
    )
    print("\n[OK] Step 4 Complete — Existing Agent Reused in Fresh Isolated Session:")
    print(f"  reused agent_id          = {resolved_agent_id}")
    print(f"  new interaction_id       = {session_b.interaction_id}")
    print(f"  previous_interaction_id  = {session_b.previous_interaction_id}")
    print(f"  Session A environment_id = {session_a_env_id}")
    print(f"  Session B environment_id = {session_b.environment_id} (isolated={isolated})")
    print("\nClean up when finished: python cleanup_managed_agent.py")
    return session_b


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Step 4: Start a new independent interaction reusing the existing agent."
    )
    parser.add_argument("--agent-id", help="Override Managed Agent ID from Step 1.")
    args = parser.parse_args()
    run_step4(agent_id=args.agent_id)


if __name__ == "__main__":
    main()
