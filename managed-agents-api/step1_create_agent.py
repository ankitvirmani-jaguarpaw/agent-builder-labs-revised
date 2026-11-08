"""Step 1: Create a reusable Managed Agent (`client.agents.create`).

Works out-of-the-box in a fresh/empty Qwiklabs Google Cloud project:
- Stages the bundled `./skills/data-analytics/SKILL.md` to Cloud Storage
  (`gs://<PROJECT_ID>-managed-agent-skills/data-analytics/SKILL.md`) and
  mounts it via `base_environment.sources` (`type="gcs"`).
- Enables sandboxed `code_execution` with outbound network access (`domain: '*'`;
  per-interaction BigQuery OAuth headers are injected at the host egress proxy).
- Persists `agent_id` to `.lab_state.json` for Steps 2, 3, and 4.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

from dotenv import load_dotenv
from low_infra_harness import (
    LAB_DIR,
    create_genai_client,
    get_gcp_credentials,
    resolve_project_id,
    save_lab_state,
)

DEFAULT_BASE_AGENT = "antigravity-preview-05-2026"
SKILL_FILE = LAB_DIR / "skills" / "data-analytics" / "SKILL.md"


def stage_skill_to_gcs(
    project_id: str,
    bucket_name: str,
    *,
    location: str = "US-CENTRAL1",
    skill_path: pathlib.Path = SKILL_FILE,
) -> str:
    """Ensures `gs://{bucket_name}` exists and uploads `data-analytics/SKILL.md`."""
    clean_bucket = bucket_name.removeprefix("gs://").strip("/")
    creds = get_gcp_credentials(["https://www.googleapis.com/auth/cloud-platform"])
    auth_header = {"Authorization": f"Bearer {creds.token}"}

    # 1. Check bucket existence; create with uniform bucket-level access if missing
    bucket_url = f"https://storage.googleapis.com/storage/v1/b/{urllib.parse.quote(clean_bucket, safe='')}"
    req = urllib.request.Request(bucket_url, headers=auth_header, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=20):
            pass
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        create_url = (
            "https://storage.googleapis.com/storage/v1/b?"
            + urllib.parse.urlencode({"project": project_id})
        )
        body = json.dumps(
            {
                "name": clean_bucket,
                "location": location,
                "iamConfiguration": {"uniformBucketLevelAccess": {"enabled": True}},
            }
        ).encode("utf-8")
        create_req = urllib.request.Request(
            create_url,
            data=body,
            headers={**auth_header, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(create_req, timeout=30):
            pass

    # 2. Upload bundled SKILL.md object
    object_name = "data-analytics/SKILL.md"
    upload_url = (
        f"https://storage.googleapis.com/upload/storage/v1/b/"
        f"{urllib.parse.quote(clean_bucket, safe='')}/o?"
        + urllib.parse.urlencode({"uploadType": "media", "name": object_name})
    )
    upload_req = urllib.request.Request(
        upload_url,
        data=skill_path.read_bytes(),
        headers={**auth_header, "Content-Type": "text/markdown; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(upload_req, timeout=30):
        pass
    return f"gs://{clean_bucket}"


def build_skill_source(
    *,
    project_id: str,
    skill_gcs_bucket: str | None = None,
    skill_resource_name: str | None = None,
) -> dict[str, str]:
    """Builds a `base_environment.sources` entry (`gcs` or `skill_registry`).

    Note: `client.agents.create` (`base_environment.sources`) only accepts
    `gcs` or `skill_registry` (`inline` is rejected with HTTP 400).
    """
    if skill_resource_name:
        return {
            "type": "skill_registry",
            "source": skill_resource_name,
            "target": "./skills",
        }
    bucket = skill_gcs_bucket or f"{project_id}-managed-agent-skills"
    bucket_uri = bucket if bucket.startswith("gs://") else f"gs://{bucket}"
    return {
        "type": "gcs",
        "source": bucket_uri.rstrip("/"),
        "target": "./skills",
    }


def build_egress_allowlist(catalog_mcp_url: str | None = None) -> list[dict[str, str]]:
    """Builds the `base_environment.network.allowlist` payload for `client.agents.create`.

    Note: `client.agents.create` currently requires `domain: '*'` on
    `base_environment.network.allowlist` (per-domain `transform` headers are
    applied at interaction time via `client.interactions.create`).
    """
    _ = catalog_mcp_url
    return [{"domain": "*"}]


def build_agent_spec(
    *,
    agent_id: str,
    project_id: str,
    catalog_mcp_url: str | None = None,
    base_agent: str = DEFAULT_BASE_AGENT,
    model: str | None = None,
    max_total_tokens: int | None = None,
    skill_gcs_bucket: str | None = None,
    skill_resource_name: str | None = None,
) -> dict[str, Any]:
    """Constructs the declarative `client.agents.create` payload."""
    system_instruction = (
        "You are an enterprise Data Analytics Agent governed by strict SQL partition and cost rules. "
        "Read and follow the mounted data-analytics skill (in ./skills/), "
        "including all immutable PROTECTED guardrails. "
        "Never run BigQuery jobs for metric formulas or partition rules—resolve those directly "
        "from the mounted skill definitions (and knowledge-catalog-mcp if attached). "
        "When live BigQuery rows are requested, build and execute /tmp/bq_runner.py (mirrored "
        "to ./bq_runner.py) inside code_execution against https://bigquery.googleapis.com "
        f"(billing project '{project_id}', dryRun<=100MB first, _TABLE_SUFFIX required, "
        "no SELECT *); outbound Authorization headers are injected automatically by the "
        "sandbox egress proxy."
    )

    tools: list[dict[str, Any]] = [{"type": "code_execution"}]
    if catalog_mcp_url:
        tools.append(
            {
                "type": "mcp_server",
                "name": "knowledge-catalog-mcp",
                "url": catalog_mcp_url,
            }
        )

    skill_source = build_skill_source(
        project_id=project_id,
        skill_gcs_bucket=skill_gcs_bucket,
        skill_resource_name=skill_resource_name,
    )

    spec: dict[str, Any] = {
        "id": agent_id,
        "base_agent": base_agent,
        "description": (
            "Data analytics agent that mounts SKILL.md and builds its own "
            "sandboxed BigQuery REST CLI."
        ),
        "system_instruction": system_instruction,
        "tools": tools,
        "base_environment": {
            "type": "remote",
            "env": {
                "GOOGLE_CLOUD_PROJECT": project_id,
            },
            "sources": [skill_source],
            "network": {
                "allowlist": build_egress_allowlist(catalog_mcp_url),
            },
        },
    }

    if model is not None or max_total_tokens is not None:
        agent_config: dict[str, Any] = {"type": "antigravity"}
        if model is not None:
            agent_config["model"] = model
        if max_total_tokens is not None:
            agent_config["max_total_tokens"] = max_total_tokens
        spec["agent_config"] = agent_config

    return spec


def wait_for_agent_ready(
    client: Any,
    agent_id: str,
    *,
    op: Any | None = None,
    timeout_sec: int = 600,
) -> Any:
    """Polls the CreateAgent LRO (and `client.agents.get`) until ready.

    Initial `base_environment` snapshot creation typically takes ~5–6 minutes.
    """
    start = time.monotonic()
    deadline = start + timeout_sec
    op_name = getattr(op, "name", None) if op is not None else None

    if isinstance(op_name, str) and op_name.startswith("projects/"):
        creds = get_gcp_credentials(["https://www.googleapis.com/auth/cloud-platform"])
        op_url = f"https://aiplatform.googleapis.com/v1beta1/{op_name}"
        next_log = 15
        while time.monotonic() < deadline:
            creds = get_gcp_credentials(
                ["https://www.googleapis.com/auth/cloud-platform"]
            )
            req = urllib.request.Request(
                op_url,
                headers={"Authorization": f"Bearer {creds.token}"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                op_data = json.loads(resp.read().decode("utf-8"))
            if op_data.get("error"):
                raise RuntimeError(
                    f"Managed Agent creation failed for '{agent_id}': {op_data['error']}"
                )
            if op_data.get("done"):
                break
            elapsed = int(time.monotonic() - start)
            if elapsed >= next_log:
                print(
                    f"  ... provisioning Managed Agent base_environment snapshot ({elapsed}s elapsed) ..."
                )
                next_log += 30
            time.sleep(10)
        else:
            raise RuntimeError(
                f"Timed out after {timeout_sec}s waiting for LRO '{op_name}'."
            )

    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            details = client.agents.get(id=agent_id)
            if getattr(details, "id", None) == agent_id:
                return details
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
        time.sleep(3)
    if last_exc is not None:
        raise RuntimeError(
            f"Timed out waiting for agent '{agent_id}': {last_exc}"
        ) from last_exc
    return client.agents.get(id=agent_id)


def run_step1(agent_id: str | None = None) -> str:
    """Stages `SKILL.md` to GCS, creates the Managed Agent, and saves `.lab_state.json`."""
    load_dotenv(LAB_DIR / ".env")

    project_id = resolve_project_id()
    catalog_mcp_url = os.getenv("CATALOG_MCP_URL") or None
    base_agent = os.getenv("MANAGED_BASE_AGENT", DEFAULT_BASE_AGENT)
    chosen_id = (
        agent_id
        or os.getenv("MANAGED_AGENT_ID")
        or f"data-analytics-{uuid.uuid4().hex[:8]}"
    )
    model = os.getenv("MANAGED_AGENT_MODEL") or None
    max_tokens_raw = os.getenv("MANAGED_AGENT_MAX_TOKENS")
    max_total_tokens = int(max_tokens_raw) if max_tokens_raw else None
    skill_resource_name = os.getenv("SKILL_RESOURCE_NAME") or None
    skill_gcs_bucket = (
        os.getenv("AGENT_SKILL_GCS_BUCKET") or f"{project_id}-managed-agent-skills"
    )

    if not skill_resource_name:
        staged_uri = stage_skill_to_gcs(project_id, skill_gcs_bucket)
    else:
        staged_uri = skill_resource_name

    client = create_genai_client(project_id, location="global")
    spec = build_agent_spec(
        agent_id=chosen_id,
        project_id=project_id,
        catalog_mcp_url=catalog_mcp_url,
        base_agent=base_agent,
        model=model,
        max_total_tokens=max_total_tokens,
        skill_gcs_bucket=skill_gcs_bucket,
        skill_resource_name=skill_resource_name,
    )

    skill_mode = spec["base_environment"]["sources"][0]["type"]
    print("=== Step 1: Creating Managed Agent (`client.agents.create`) ===")
    print(f"  Project:      {project_id}")
    print(f"  Agent ID:     {chosen_id}")
    print(f"  Base Agent:   {base_agent}")
    print(f"  Skill Source: {skill_mode} ({staged_uri})")

    op = client.agents.create(**spec)
    print(f"  Operation:    {getattr(op, 'name', op)}")

    details = wait_for_agent_ready(client, chosen_id, op=op)
    tools_summary = [
        getattr(t, "type", str(t)) for t in (getattr(details, "tools", None) or [])
    ]
    save_lab_state(
        {
            "project_id": project_id,
            "agent_id": details.id,
            "skill_gcs_bucket": skill_gcs_bucket.removeprefix("gs://").strip("/"),
        }
    )

    print("\n[OK] Managed Agent Ready!")
    print(f"  agent_id = {details.id}")
    print(f"  tools    = {tools_summary}")
    print("\nNext step: python step2_first_interaction.py")
    return details.id


def main() -> None:
    parser = argparse.ArgumentParser(description="Step 1: Create the Managed Agent.")
    parser.add_argument("--agent-id", help="Optional custom ID for the Managed Agent.")
    args = parser.parse_args()
    run_step1(agent_id=args.agent_id)


if __name__ == "__main__":
    main()
