"""Deletes the Managed Agent and remote sandboxes created during Steps 1–4."""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from dotenv import load_dotenv
from low_infra_harness import (
    LAB_DIR,
    clear_lab_state,
    create_genai_client,
    get_gcp_credentials,
    load_lab_state,
    resolve_project_id,
)


def _delete_gcs_bucket(bucket_name: str) -> None:
    clean = bucket_name.removeprefix("gs://").strip("/")
    creds = get_gcp_credentials(["https://www.googleapis.com/auth/cloud-platform"])
    headers = {"Authorization": f"Bearer {creds.token}"}
    list_url = f"https://storage.googleapis.com/storage/v1/b/{urllib.parse.quote(clean, safe='')}/o"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(list_url, headers=headers, method="GET"),
            timeout=20,
        ) as resp:
            items = json.loads(resp.read().decode("utf-8")).get("items") or []
        for item in items:
            obj_url = (
                f"https://storage.googleapis.com/storage/v1/b/"
                f"{urllib.parse.quote(clean, safe='')}/o/"
                f"{urllib.parse.quote(item['name'], safe='')}"
            )
            with urllib.request.urlopen(
                urllib.request.Request(obj_url, headers=headers, method="DELETE"),
                timeout=20,
            ):
                pass
        bkt_url = f"https://storage.googleapis.com/storage/v1/b/{urllib.parse.quote(clean, safe='')}"
        with urllib.request.urlopen(
            urllib.request.Request(bkt_url, headers=headers, method="DELETE"),
            timeout=20,
        ):
            pass
        print(f"Deleted Skill GCS Bucket: gs://{clean}")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            print(f"Note deleting bucket gs://{clean}: {exc}")


def main() -> None:
    load_dotenv(LAB_DIR / ".env")
    parser = argparse.ArgumentParser(
        description="Delete the Managed Agent and persistent remote sandboxes from this lab."
    )
    parser.add_argument(
        "positional_agent_id",
        nargs="?",
        help="Optional Agent ID to delete (defaults to .lab_state.json or MANAGED_AGENT_ID).",
    )
    parser.add_argument("--agent-id", dest="agent_id", help="Agent ID to delete.")
    parser.add_argument(
        "--env-id",
        action="append",
        dest="env_ids",
        help="Optional remote sandbox environment_id(s) to delete.",
    )
    parser.add_argument(
        "--delete-bucket",
        action="store_true",
        help="Also delete the staged skill Cloud Storage bucket.",
    )
    args = parser.parse_args()

    project_id = resolve_project_id()
    state = load_lab_state()
    agent_id = (
        args.agent_id
        or args.positional_agent_id
        or os.getenv("MANAGED_AGENT_ID")
        or state.get("agent_id")
    )

    env_ids: list[str] = []
    for eid in (args.env_ids or []) + list(state.get("environments") or []):
        if eid and eid not in env_ids:
            env_ids.append(eid)
    env_from_var = os.getenv("MANAGED_ENVIRONMENT_ID")
    if env_from_var and env_from_var not in env_ids:
        env_ids.append(env_from_var)

    if not agent_id and not env_ids:
        raise ValueError(
            "Nothing to clean up: no .lab_state.json found and no --agent-id / --env-id supplied."
        )

    client = create_genai_client(project_id, location="global")

    if agent_id:
        client.agents.delete(id=agent_id)
        print(f"Deleted Managed Agent: {agent_id}")

    for eid in env_ids:
        try:
            client.environments.delete(id=eid)
            print(f"Deleted Remote Environment: {eid}")
        except Exception as exc:  # noqa: BLE001
            if "Method not visible to labels: {PUBLIC}" in str(exc):
                print(
                    f"Remote Environment {eid}: managed 7-day auto-TTL (released with agent)"
                )
            else:
                print(f"Note deleting environment {eid}: {exc}")

    skill_bucket = os.getenv("AGENT_SKILL_GCS_BUCKET") or state.get("skill_gcs_bucket")
    if args.delete_bucket and skill_bucket:
        _delete_gcs_bucket(str(skill_bucket))

    clear_lab_state()

    listed = client.agents.list()
    remaining = [a.id for a in (getattr(listed, "agents", None) or [])]
    print(f"Remaining project agents: {remaining or 'None'}")


if __name__ == "__main__":
    main()
