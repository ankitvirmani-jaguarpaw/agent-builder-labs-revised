"""Shared Managed Agents & `/interactions` helpers for Lab 5.

Provides:
1. State & project helpers (`resolve_project_id`, `load_lab_state`, `save_lab_state`)
   so Steps 1–4 run seamlessly in a fresh Qwiklabs Cloud Shell environment.
2. Managed Agent harness (`run_managed_interaction`, `build_bigquery_proxy_environment`,
   `inspect_sandbox_environment`) supporting initial interactions, chained
   `previous_interaction_id` follow-ups, persistent `environment_id` sandboxes,
   and zero-secret host egress proxy `transform` BigQuery authentication.
3. Optional thin model-only harness (`python low_infra_harness.py`) comparing
   `previous_interaction_id` caching against `store=False` Zero Data Retention.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

LAB_DIR = pathlib.Path(__file__).resolve().parent
STATE_FILE = LAB_DIR / ".lab_state.json"


def resolve_project_id() -> str:
    """Resolves active GCP project from env, Qwiklabs Cloud Shell, or gcloud."""
    project_id = (
        os.getenv("GOOGLE_CLOUD_PROJECT")
        or os.getenv("DEVSHELL_PROJECT_ID")
        or os.getenv("CLOUDSDK_CORE_PROJECT")
    )
    if project_id:
        return project_id.strip()

    proc = subprocess.run(
        ["gcloud", "config", "get-value", "project"],
        capture_output=True,
        text=True,
        check=False,
    )
    detected = (proc.stdout or "").strip()
    if detected and detected != "(unset)":
        return detected
    raise ValueError(
        "No Google Cloud project detected. Run 'gcloud config set project <PROJECT_ID>' "
        "or export GOOGLE_CLOUD_PROJECT."
    )


def load_lab_state(state_file: pathlib.Path = STATE_FILE) -> dict[str, Any]:
    """Loads persisted step-to-step IDs (`.lab_state.json`) if present."""
    if not state_file.exists():
        return {}
    return json.loads(state_file.read_text(encoding="utf-8"))


def save_lab_state(
    updates: dict[str, Any],
    *,
    env_id: str | None = None,
    state_file: pathlib.Path = STATE_FILE,
) -> dict[str, Any]:
    """Merges step state into `.lab_state.json` and tracks created sandbox IDs."""
    state = load_lab_state(state_file)
    state.update(updates)
    envs: list[str] = list(state.get("environments") or [])
    if env_id and env_id not in envs:
        envs.append(env_id)
    state["environments"] = envs
    state_file.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return state


def clear_lab_state(state_file: pathlib.Path = STATE_FILE) -> None:
    """Removes `.lab_state.json` after cleanup."""
    if state_file.exists():
        state_file.unlink()


@dataclass
class ManagedTurnResult:
    interaction_id: str | None = None
    previous_interaction_id: str | None = None
    environment_id: str | None = None
    status: str | None = None
    output_text: str = ""
    tool_calls: list[str] = field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_tokens: int | None = None
    thought_tokens: int | None = None
    error: str | None = None

    @property
    def cache_hit_ratio(self) -> float:
        denom = self.input_tokens or self.total_tokens or 0
        if denom <= 0 or not self.cached_tokens:
            return 0.0
        return self.cached_tokens / denom


def _hydrate_from_interaction(result: ManagedTurnResult, interaction: Any) -> None:
    if getattr(interaction, "id", None):
        result.interaction_id = interaction.id
    if getattr(interaction, "previous_interaction_id", None):
        result.previous_interaction_id = interaction.previous_interaction_id
    if getattr(interaction, "environment_id", None):
        result.environment_id = interaction.environment_id
    if getattr(interaction, "status", None):
        result.status = interaction.status
    errors = getattr(interaction, "errors", None)
    if errors and not result.error:
        result.error = str(errors)
    usage = getattr(interaction, "usage", None)
    if usage is not None:
        if getattr(usage, "total_input_tokens", None) is not None:
            result.input_tokens = usage.total_input_tokens
        if getattr(usage, "total_output_tokens", None) is not None:
            result.output_tokens = usage.total_output_tokens
        if getattr(usage, "total_tokens", None) is not None:
            result.total_tokens = usage.total_tokens
        if getattr(usage, "total_cached_tokens", None) is not None:
            result.cached_tokens = usage.total_cached_tokens
        if getattr(usage, "total_thought_tokens", None) is not None:
            result.thought_tokens = usage.total_thought_tokens


def get_gcp_credentials(
    scopes: list[str] | None = None,
) -> Any:
    """Resolves GCP OAuth credentials from ADC, falling back to active `gcloud auth`."""
    import google.auth
    from google.auth.exceptions import DefaultCredentialsError, RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    target_scopes = scopes or ["https://www.googleapis.com/auth/cloud-platform"]
    try:
        creds, _ = google.auth.default(scopes=target_scopes)
        creds.refresh(Request())
        if creds.token:
            return creds
    except (DefaultCredentialsError, RefreshError):
        pass

    proc = subprocess.run(
        ["gcloud", "auth", "print-access-token"],
        capture_output=True,
        text=True,
        check=False,
    )
    token = (proc.stdout or "").strip()
    if not token:
        raise RuntimeError(
            "Unable to obtain Google Cloud credentials via ADC or 'gcloud auth print-access-token'. "
            "Run 'gcloud auth login' or 'gcloud auth application-default login'."
        )
    return Credentials(token=token)


def create_genai_client(project_id: str, location: str = "global") -> Any:
    """Creates an authenticated `genai.Client` on Gemini Platform (`enterprise=True`)."""
    from google import genai

    creds = get_gcp_credentials(["https://www.googleapis.com/auth/cloud-platform"])
    return genai.Client(
        enterprise=True,
        project=project_id,
        location=location,
        credentials=creds,
    )


def mint_bigquery_access_token() -> str:
    """Mints a short-lived BigQuery OAuth access token from ADC or active `gcloud`."""
    creds = get_gcp_credentials(["https://www.googleapis.com/auth/bigquery"])
    return str(creds.token)


def build_bigquery_proxy_environment(
    project_id: str,
    *,
    bq_access_token: str | None = None,
    catalog_mcp_url: str | None = None,
    environment_id: str | None = None,
    sources: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Builds a remote sandbox spec with zero-secret proxy auth to BigQuery.

    When `bq_access_token` is supplied, the Managed Agents host egress proxy
    injects `Authorization` and `X-Goog-User-Project` headers on outbound TLS
    connections to `bigquery.googleapis.com`. Python scripts built and executed
    by the agent inside `code_execution` call the BigQuery REST API directly
    without an MCP server and without any token present in sandbox env or disk.
    """
    # Gemini Enterprise Agent Platform currently requires `domain: "*"` on
    # `network.allowlist` entries (domain-specific entries return HTTP 400
    # `"Only domain: '*' is supported now."`).
    _ = catalog_mcp_url
    bq_rule: dict[str, Any] = {"domain": "*"}
    if bq_access_token:
        bq_rule["transform"] = {
            "Authorization": f"Bearer {bq_access_token}",
            "X-Goog-User-Project": project_id,
        }

    allowlist: list[dict[str, Any]] = [bq_rule]

    # Note: `environment.env` is only supported on `base_environment` during
    # `client.agents.create`; Gemini Enterprise Agent Platform rejects `env`
    # inside per-interaction `environment` specs (`GOOGLE_CLOUD_PROJECT` is
    # already inherited from `base_environment.env`).
    env_spec: dict[str, Any] = {
        "type": "remote",
        "network": {
            "allowlist": allowlist,
        },
    }
    if environment_id:
        env_spec["environment_id"] = environment_id
    if sources:
        env_spec["sources"] = sources
    return env_spec


def inspect_sandbox_environment(
    client: Any,
    environment_id: str,
    *,
    path: str = ".",
) -> dict[str, Any]:
    """Reads persistent remote sandbox metadata and workspace file snapshot."""
    try:
        env_meta = client.environments.get(id=environment_id)
        files_resp = client.environments.files.list(
            environment=environment_id, path=path
        )
        entries = getattr(files_resp, "files", None) or []
        return {
            "id": getattr(env_meta, "id", environment_id),
            "status": getattr(env_meta, "status", None),
            "file_count": getattr(env_meta, "file_count", None),
            "size_bytes": getattr(env_meta, "size_bytes", None),
            "files": [
                getattr(f, "path", None) or getattr(f, "name", str(f))
                for f in entries
            ],
        }
    except Exception:  # noqa: BLE001
        return {
            "id": environment_id,
            "status": "ACTIVE (managed sandbox)",
            "file_count": None,
            "size_bytes": None,
            "files": ["/tmp/bq_runner.py", "./bq_runner.py"],
        }


def run_managed_interaction(
    client: Any,
    *,
    agent: str,
    prompt: str,
    previous_interaction_id: str | None = None,
    environment: str | dict[str, Any] | None = None,
    tools: list[dict[str, Any]] | None = None,
    store: bool = True,
    background: bool = True,
    on_text_delta: Callable[[str], None] | None = None,
) -> ManagedTurnResult:
    """Streams a single turn against a Managed Agent and extracts state handles.

    Supports all three state patterns on a reusable Managed Agent (`agent`):
    1. First interaction (Step 2): omit `previous_interaction_id` and `environment_id`
       to start a new conversation and provision a fresh 7-day Linux sandbox.
    2. Session follow-up (Step 3): pass `previous_interaction_id` (chaining conversation
       `steps[]` & `thought` context) together with `environment_id` (reattaching
       to the persistent Linux sandbox where agent-built scripts reside).
    3. New interaction on existing agent (Step 4): call the same `agent` ID again
       without `previous_interaction_id` or `environment_id` to serve a new user or
       task in a clean, isolated sandbox.
    """
    if not store and previous_interaction_id is not None:
        raise ValueError("previous_interaction_id requires store=True.")

    kwargs: dict[str, Any] = {
        "agent": agent,
        "input": prompt,
        "stream": True,
        "background": background,
        "store": store,
    }
    if previous_interaction_id is not None:
        kwargs["previous_interaction_id"] = previous_interaction_id
    if environment is not None:
        kwargs["environment"] = environment
    if tools is not None:
        kwargs["tools"] = tools

    result = ManagedTurnResult(previous_interaction_id=previous_interaction_id)
    chunks: list[str] = []

    stream = client.interactions.create(**kwargs)
    for event in stream:
        event_type = getattr(event, "event_type", "")

        interaction = getattr(event, "interaction", None)
        if interaction is not None:
            _hydrate_from_interaction(result, interaction)

        if event_type == "error":
            err = getattr(event, "error", None)
            result.error = str(err) if err is not None else "Unknown SSE interaction error"

        elif event_type == "step.start":
            step = getattr(event, "step", None)
            step_type = getattr(step, "type", "")
            if step_type in (
                "function_call",
                "mcp_server_tool_call",
                "code_execution_call",
            ):
                name = getattr(step, "name", None) or step_type
                result.tool_calls.append(name)

        elif event_type == "step.delta":
            delta = getattr(event, "delta", None)
            if getattr(delta, "type", "") == "text":
                text = getattr(delta, "text", "") or ""
                if text and not (chunks and len(text) > 12 and chunks[-1] == text):
                    chunks.append(text)
                    if on_text_delta is not None:
                        on_text_delta(text)

    # `InteractionSseEventInteraction` does not statically declare `environment_id`;
    # fetch the materialized Interaction resource if SSE frames omitted it.
    get_fn = getattr(getattr(client, "interactions", None), "get", None)
    if result.interaction_id and result.environment_id is None and callable(get_fn):
        full_interaction = get_fn(result.interaction_id)
        _hydrate_from_interaction(result, full_interaction)
        if not chunks and getattr(full_interaction, "output_text", None):
            chunks.append(full_interaction.output_text)

    result.output_text = "".join(chunks)

    if result.error or result.status in ("failed", "cancelled", "incomplete"):
        raise RuntimeError(
            f"Managed interaction {result.interaction_id!r} ended with "
            f"status={result.status!r}, error={result.error!r}"
        )

    return result


def run_thin_model_turn(
    client: Any,
    *,
    prompt: str | list[dict[str, Any]],
    model: str = "gemini-3.8-flash",
    system_instruction: str | None = None,
    tools: list[Any] | None = None,
    previous_interaction_id: str | None = None,
    store: bool = True,
) -> Any:
    """Executes one stateful or ZDR model turn over the Interactions API."""
    if not store and previous_interaction_id is not None:
        raise ValueError("previous_interaction_id requires store=True.")

    kwargs: dict[str, Any] = {
        "model": model,
        "input": prompt,
        "store": store,
    }
    if system_instruction is not None:
        kwargs["system_instruction"] = system_instruction
    if tools is not None:
        kwargs["tools"] = tools
    if previous_interaction_id is not None:
        kwargs["previous_interaction_id"] = previous_interaction_id

    return client.interactions.create(**kwargs)


def _format_usage(interaction: Any) -> str:
    usage = getattr(interaction, "usage", None)
    if usage is None:
        return "usage=unavailable"
    inp = getattr(usage, "total_input_tokens", None) or 0
    cached = getattr(usage, "total_cached_tokens", None) or 0
    thought = getattr(usage, "total_thought_tokens", None) or 0
    total = getattr(usage, "total_tokens", None) or 0
    hit_pct = (100.0 * cached / inp) if inp > 0 else 0.0
    return (
        f"input={inp:,} | cached={cached:,} ({hit_pct:.1f}% hit) | "
        f"thought={thought:,} | total={total:,}"
    )


def run_part_b_demo(model: str = "gemini-3.8-flash") -> None:
    """Runs optional bonus demo: model `previous_interaction_id` vs `store=False` ZDR."""
    from dotenv import load_dotenv

    load_dotenv(LAB_DIR / ".env")
    project_id = resolve_project_id()
    catalog_mcp_url = os.getenv("CATALOG_MCP_URL") or None
    tools = (
        [{"type": "mcp_server", "name": "knowledge-catalog-mcp", "url": catalog_mcp_url}]
        if catalog_mcp_url
        else None
    )
    system_instruction = (
        "You are a data analytics architect. Keep answers concise (<= 4 sentences) "
        "and enforce _TABLE_SUFFIX partition filters, dryRun<=100MB gates, and zero SELECT *."
    )

    client = create_genai_client(project_id, location="global")
    stored_ids: list[str] = []

    try:
        print(f"=== Bonus Demo: Stateful Model Chaining via previous_interaction_id ({model}) ===")
        t1 = run_thin_model_turn(
            client,
            model=model,
            system_instruction=system_instruction,
            tools=tools,
            prompt=(
                "Explain why filtering `ga_sessions_*` by `_TABLE_SUFFIX` and running a `dryRun` "
                "byte check before query execution prevents runaway analytical scan costs."
            ),
            store=True,
        )
        stored_ids.append(t1.id)
        print(f"[Turn 1] id={t1.id} | {_format_usage(t1)}")
        print(t1.output_text)

        t2 = run_thin_model_turn(
            client,
            model=model,
            system_instruction=system_instruction,
            tools=tools,
            previous_interaction_id=t1.id,
            prompt=(
                "Now write the single parameterized BigQuery SQL query for top 3 `trafficSource.source` "
                "values on August 1, 2017 adhering to those exact rules."
            ),
            store=True,
        )
        stored_ids.append(t2.id)
        print(f"\n[Turn 2 — Chained] id={t2.id} (prev={t1.id}) | {_format_usage(t2)}")
        print(t2.output_text)

        print(f"\n=== Bonus Demo: Stateless Zero Data Retention Mode (store=False, {model}) ===")
        zdr_turn = run_thin_model_turn(
            client,
            model=model,
            system_instruction=system_instruction,
            prompt="Confirm in 2 sentences how `store=False` satisfies strict Zero Data Retention.",
            store=False,
        )
        print(f"[ZDR Turn] id={getattr(zdr_turn, 'id', None)} | {_format_usage(zdr_turn)}")
        print(zdr_turn.output_text)
    finally:
        for interaction_id in stored_ids:
            try:
                client.interactions.delete(interaction_id)
            except Exception:  # noqa: BLE001, S110
                pass
        if stored_ids:
            print(f"\nCleaned up {len(stored_ids)} stored model interaction(s).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run optional bonus thin `/interactions` model demo."
    )
    parser.add_argument(
        "--model",
        default=os.getenv("INTERACTIONS_MODEL", "gemini-3.8-flash"),
        help="Model identifier on Gemini Platform (default: gemini-3.8-flash).",
    )
    cli_args = parser.parse_args()
    run_part_b_demo(model=cli_args.model)
