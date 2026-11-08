"""Offline contract tests (`pytest`) & full 4-step runner (`python test_managed_agent.py`).

Usage:
  1. Fast hermetic unit & Qwiklabs state-handoff tests (zero cloud calls):
       pytest test_managed_agent.py
  2. Live end-to-end execution of Steps 1 -> 2 -> 3 -> 4 against Gemini Platform:
       python test_managed_agent.py [--cleanup]
"""

from __future__ import annotations

import argparse
import pathlib
import types

import pytest
from low_infra_harness import (
    build_bigquery_proxy_environment,
    clear_lab_state,
    inspect_sandbox_environment,
    load_lab_state,
    run_managed_interaction,
    run_thin_model_turn,
    save_lab_state,
)
from step1_create_agent import (
    DEFAULT_BASE_AGENT,
    SKILL_FILE,
    build_agent_spec,
    build_egress_allowlist,
    build_skill_source,
    run_step1,
)
from step2_first_interaction import run_step2
from step3_followup_interaction import run_step3
from step4_new_interaction import run_step4

# ---------------------------------------------------------------------------
# Hermetic Unit & Contract Tests (`pytest test_managed_agent.py`)
# ---------------------------------------------------------------------------


def test_skill_markdown_is_self_contained_for_fresh_qwiklabs() -> None:
    text = SKILL_FILE.read_text(encoding="utf-8")
    assert text.startswith("---\nname: data-analytics\n")
    assert "bounce_rate" in text
    assert "monthly_active_users" in text
    assert "COUNT(DISTINCT fullVisitorId)" in text
    assert "<!-- PROTECTED:START -->" in text
    assert "<!-- PROTECTED:END -->" in text
    assert "/tmp/bq_runner.py" in text
    assert "./bq_runner.py" in text
    assert "dryRun" in text
    assert "_TABLE_SUFFIX" in text


def test_build_skill_source_uses_gcs_or_skill_registry_for_agent_create() -> None:
    default_gcs = build_skill_source(project_id="qwiklabs-gcp-00-demo")
    assert default_gcs == {
        "type": "gcs",
        "source": "gs://qwiklabs-gcp-00-demo-managed-agent-skills",
        "target": "./skills",
    }

    custom_gcs = build_skill_source(
        project_id="qwiklabs-gcp-00-demo",
        skill_gcs_bucket="qwiklabs-proj-skills",
    )
    assert custom_gcs == {
        "type": "gcs",
        "source": "gs://qwiklabs-proj-skills",
        "target": "./skills",
    }

    reg = build_skill_source(
        project_id="qwiklabs-gcp-00-demo",
        skill_resource_name="projects/p/locations/global/skills/s1",
    )
    assert reg == {
        "type": "skill_registry",
        "source": "projects/p/locations/global/skills/s1",
        "target": "./skills",
    }


def test_build_agent_spec_works_standalone_without_mcp_and_with_optional_mcp() -> None:
    # Fresh/empty Qwiklabs mode: zero external MCP required
    standalone = build_agent_spec(
        agent_id="qwiklabs-agent-01",
        project_id="qwiklabs-gcp-00-demo",
    )
    assert standalone["id"] == "qwiklabs-agent-01"
    assert standalone["base_agent"] == DEFAULT_BASE_AGENT
    assert standalone["tools"] == [{"type": "code_execution"}]
    assert standalone["base_environment"]["network"]["allowlist"] == [
        {"domain": "*"}
    ]

    # Optional MCP mode when Lab 2 Cloud Run server is also present
    mcp_url = "https://knowledge-catalog-mcp-abc123-uc.a.run.app/sse"
    with_mcp = build_agent_spec(
        agent_id="qwiklabs-agent-02",
        project_id="qwiklabs-gcp-00-demo",
        catalog_mcp_url=mcp_url,
        model="gemini-3.8-flash",
        max_total_tokens=65536,
    )
    assert with_mcp["agent_config"] == {
        "type": "antigravity",
        "model": "gemini-3.8-flash",
        "max_total_tokens": 65536,
    }
    assert {
        "type": "mcp_server",
        "name": "knowledge-catalog-mcp",
        "url": mcp_url,
    } in with_mcp["tools"]
    assert with_mcp["base_environment"]["network"]["allowlist"] == [
        {"domain": "*"}
    ]


def test_lab_state_persistence_across_steps(tmp_path: pathlib.Path) -> None:
    state_path = tmp_path / ".lab_state.json"
    assert load_lab_state(state_path) == {}

    save_lab_state({"agent_id": "agent-1"}, state_file=state_path)
    save_lab_state(
        {
            "session_a_interaction_id": "int-a1",
            "session_a_environment_id": "env-a",
        },
        env_id="env-a",
        state_file=state_path,
    )
    state = save_lab_state(
        {
            "session_b_interaction_id": "int-b1",
            "session_b_environment_id": "env-b",
        },
        env_id="env-b",
        state_file=state_path,
    )
    assert state["agent_id"] == "agent-1"
    assert state["session_a_interaction_id"] == "int-a1"
    assert state["environments"] == ["env-a", "env-b"]

    clear_lab_state(state_path)
    assert not state_path.exists()


def test_build_bigquery_proxy_environment_injects_headers_outside_sandbox() -> None:
    env_spec = build_bigquery_proxy_environment(
        "qwiklabs-gcp-01-demo",
        bq_access_token="stub-ephemeral-token",
        environment_id="env_persist_001",
    )
    assert env_spec["type"] == "remote"
    assert env_spec["environment_id"] == "env_persist_001"
    assert "env" not in env_spec

    allowlist = env_spec["network"]["allowlist"]
    bq_entry = allowlist[0]
    assert bq_entry["domain"] == "*"
    assert bq_entry["transform"] == {
        "Authorization": "Bearer stub-ephemeral-token",
        "X-Goog-User-Project": "qwiklabs-gcp-01-demo",
    }


def test_run_managed_interaction_hydrates_env_id_via_get_fallback() -> None:
    events = [
        types.SimpleNamespace(
            event_type="interaction.created",
            interaction=types.SimpleNamespace(
                id="int_1", status="in_progress", usage=None
            ),
        ),
        types.SimpleNamespace(
            event_type="step.start",
            step=types.SimpleNamespace(type="code_execution_call", name=None),
        ),
        types.SimpleNamespace(
            event_type="step.delta",
            delta=types.SimpleNamespace(type="text", text="Top source: (direct)."),
        ),
        types.SimpleNamespace(
            event_type="interaction.completed",
            interaction=types.SimpleNamespace(
                id="int_1",
                status="completed",
                usage=types.SimpleNamespace(
                    total_input_tokens=4000,
                    total_output_tokens=200,
                    total_tokens=4200,
                    total_cached_tokens=3600,
                    total_thought_tokens=310,
                ),
            ),
        ),
    ]

    fetched_ids: list[str] = []

    def fake_get(interaction_id: str):
        fetched_ids.append(interaction_id)
        return types.SimpleNamespace(
            id=interaction_id,
            environment_id="env_persist_001",
            status="completed",
            usage=None,
        )

    fake_client = types.SimpleNamespace(
        interactions=types.SimpleNamespace(
            create=lambda **_kw: iter(events),
            get=fake_get,
        )
    )

    turn = run_managed_interaction(
        fake_client,
        agent="demo-agent-01",
        prompt="Run /tmp/bq_runner.py for 20170801.",
        environment={"type": "remote", "environment_id": "env_persist_001"},
        tools=[{"type": "code_execution"}],
    )

    assert fetched_ids == ["int_1"]
    assert turn.interaction_id == "int_1"
    assert turn.environment_id == "env_persist_001"
    assert turn.cache_hit_ratio == pytest.approx(0.90)


def test_run_managed_interaction_raises_on_error_event_or_failed_status() -> None:
    error_events = [
        types.SimpleNamespace(
            event_type="interaction.created",
            interaction=types.SimpleNamespace(
                id="int_err", environment_id="env_1", status="in_progress", usage=None
            ),
        ),
        types.SimpleNamespace(
            event_type="error",
            error="Sandbox quota exhausted",
        ),
    ]
    fake_client = types.SimpleNamespace(
        interactions=types.SimpleNamespace(create=lambda **_kw: iter(error_events))
    )

    with pytest.raises(RuntimeError, match="Sandbox quota exhausted"):
        run_managed_interaction(fake_client, agent="demo-agent-01", prompt="Run query.")


def test_step3_followup_chains_previous_interaction_id_and_blocks_zdr() -> None:
    from google.genai._gaos.types.interactions import CreateAgentInteraction

    captured_calls: list[dict[str, object]] = []

    def fake_create(**kwargs: object):
        CreateAgentInteraction.model_validate(kwargs)
        captured_calls.append(dict(kwargs))
        return iter(
            [
                types.SimpleNamespace(
                    event_type="interaction.completed",
                    interaction=types.SimpleNamespace(
                        id="int_followup_2",
                        previous_interaction_id=kwargs.get("previous_interaction_id"),
                        environment_id="env_persist_001",
                        status="completed",
                        usage=None,
                    ),
                )
            ]
        )

    fake_client = types.SimpleNamespace(
        interactions=types.SimpleNamespace(create=fake_create)
    )

    with pytest.raises(ValueError, match="store=True"):
        run_managed_interaction(
            fake_client,
            agent="demo-agent-01",
            prompt="Follow up in ZDR mode",
            previous_interaction_id="int_1",
            store=False,
        )

    followup = run_managed_interaction(
        fake_client,
        agent="demo-agent-01",
        prompt="Run /tmp/bq_runner.py using our previous formula.",
        previous_interaction_id="int_1",
        environment={"type": "remote", "environment_id": "env_persist_001"},
    )
    assert captured_calls[0]["previous_interaction_id"] == "int_1"
    assert followup.previous_interaction_id == "int_1"
    assert followup.environment_id == "env_persist_001"


def test_step4_reusing_existing_agent_starts_fresh_isolated_interaction() -> None:
    captured_calls: list[dict[str, object]] = []

    def fake_create(**kwargs: object):
        call_num = len(captured_calls) + 1
        captured_calls.append(dict(kwargs))
        return iter(
            [
                types.SimpleNamespace(
                    event_type="interaction.completed",
                    interaction=types.SimpleNamespace(
                        id=f"int_session_{call_num}",
                        previous_interaction_id=kwargs.get("previous_interaction_id"),
                        environment_id=f"env_session_{call_num}",
                        status="completed",
                        usage=None,
                    ),
                )
            ]
        )

    fake_client = types.SimpleNamespace(
        interactions=types.SimpleNamespace(create=fake_create)
    )

    session_a = run_managed_interaction(
        fake_client,
        agent="demo-agent-01",
        prompt="Step 2: bounce_rate formula?",
    )
    session_b = run_managed_interaction(
        fake_client,
        agent="demo-agent-01",
        prompt="Step 4: monthly_active_users formula?",
    )

    assert captured_calls[0]["agent"] == captured_calls[1]["agent"] == "demo-agent-01"
    assert "previous_interaction_id" not in captured_calls[1]
    assert "environment" not in captured_calls[1]
    assert session_b.previous_interaction_id is None
    assert session_a.interaction_id != session_b.interaction_id
    assert session_a.environment_id != session_b.environment_id


def test_inspect_sandbox_environment_and_thin_model_turn() -> None:
    fake_client = types.SimpleNamespace(
        environments=types.SimpleNamespace(
            get=lambda *, id: types.SimpleNamespace(
                id=id, status="READY", file_count=2, size_bytes=4096
            ),
            files=types.SimpleNamespace(
                list=lambda *, environment, path: types.SimpleNamespace(
                    files=[types.SimpleNamespace(path="bq_runner.py", name="bq_runner.py")]
                )
            ),
        ),
        interactions=types.SimpleNamespace(
            create=lambda **kw: types.SimpleNamespace(id="int_m2", kw=kw)
        ),
    )
    snap = inspect_sandbox_environment(fake_client, "env_persist_001")
    assert "bq_runner.py" in snap["files"]

    with pytest.raises(ValueError, match="store=True"):
        run_thin_model_turn(
            fake_client, prompt="x", previous_interaction_id="int_m1", store=False
        )
    assert build_egress_allowlist(None) == [{"domain": "*"}]


# ---------------------------------------------------------------------------
# Live 4-Step End-to-End Runner (`python test_managed_agent.py [--cleanup]`)
# ---------------------------------------------------------------------------


def run_live_verification(cleanup: bool = False) -> None:
    try:
        agent_id = run_step1()
        t1 = run_step2(agent_id=agent_id)
        assert "_table_suffix" in t1.output_text.lower()
        assert "code_execution_call" in t1.tool_calls

        t2 = run_step3(
            agent_id=agent_id,
            previous_interaction_id=t1.interaction_id,
            environment_id=t1.environment_id,
        )
        assert "code_execution_call" in t2.tool_calls
        assert "mcp_server_tool_call" not in t2.tool_calls

        t3 = run_step4(agent_id=agent_id)
        assert t3.previous_interaction_id is None
        assert "fullvisitorid" in t3.output_text.lower()
        if t1.environment_id and t3.environment_id:
            assert t3.environment_id != t1.environment_id

        print("\nALL 4 QWIKLABS STEPS PASSED END-TO-END!")
    finally:
        if cleanup:
            import cleanup_managed_agent

            cleanup_managed_agent.main()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Delete the Managed Agent and sandboxes after running Steps 1-4.",
    )
    args = parser.parse_args()
    run_live_verification(cleanup=args.cleanup)
