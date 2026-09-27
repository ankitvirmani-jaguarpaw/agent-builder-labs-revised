"""test-mcp-server-cloud-run.py

Full end-to-end test suite for the deployed Knowledge Catalog MCP Server on Cloud Run.
Includes automatic verification and binding of required Dataplex IAM roles.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client

MCP_URL = os.getenv("CATALOG_MCP_URL")

REQUIRED_DATAPLEX_ROLES = [
    "roles/dataplex.viewer",
    "roles/dataplex.metadataReader",
]


def ensure_dataplex_permissions():
    """Checks and automatically binds Dataplex roles to the project's Compute SA."""
    print("Checking Cloud Run runtime IAM permissions...")
    try:
        # Resolve active GCP project
        project_id = subprocess.check_output(
            ["gcloud", "config", "get-value", "project"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()

        if not project_id:
            print("⚠️ Warning: No gcloud active project detected. Skipping pre-flight IAM check.")
            return

        # Resolve project number to get the default Compute Engine service account
        project_number = subprocess.check_output(
            ["gcloud", "projects", "describe", project_id, "--format=value(projectNumber)"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        compute_sa = f"{project_number}-compute@developer.gserviceaccount.com"

        for role in REQUIRED_DATAPLEX_ROLES:
            subprocess.run(
                [
                    "gcloud",
                    "projects",
                    "add-iam-policy-binding",
                    project_id,
                    f"--member=serviceAccount:{compute_sa}",
                    f"--role={role}",
                    "--condition=None",
                    "--quiet",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
            )
        print("✅ Cloud Run runtime service account verified with Dataplex roles.\n")
    except Exception as e:
        print(f"⚠️ Warning: Could not verify/apply Dataplex IAM bindings automatically: {e}\n")


async def run_mcp_tests():
    if not MCP_URL:
        print("❌ Error: Set CATALOG_MCP_URL environment variable first.")
        sys.exit(1)

    # Ensure runtime permissions are active
    ensure_dataplex_permissions()

    print("===================================================================")
    print(f" Connecting to Live Cloud Run MCP Server at: {MCP_URL}")
    print("===================================================================\n")

    async with sse_client(MCP_URL) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            print("✅ [Handshake] MCP Session successfully initialized with Cloud Run.")

            tools_response = await session.list_tools()
            tool_names = [t.name for t in tools_response.tools]
            print(f"✅ [Discovery] Discovered registered tools: {tool_names}\n")

            # -----------------------------------------------------------------
            # Test 1: Exact Metric Lookup (get_semantic_metric)
            # -----------------------------------------------------------------
            print("=" * 60)
            print("TEST 1: get_semantic_metric('bounce_rate')")
            print("=" * 60)
            res_metric = await session.call_tool(
                "get_semantic_metric",
                arguments={"metric_name": "bounce_rate"}
            )
            print("Response:")
            print(res_metric.content[0].text)

            # -----------------------------------------------------------------
            # Test 2: Table Metadata & Partition Spec (get_table_metadata)
            # -----------------------------------------------------------------
            print("\n" + "=" * 60)
            print("TEST 2: get_table_metadata('ga_sessions')")
            print("=" * 60)
            res_table = await session.call_tool(
                "get_table_metadata",
                arguments={"table_alias": "ga_sessions"}
            )
            print("Response:")
            print(res_table.content[0].text)

            # -----------------------------------------------------------------
            # Test 3: Catalog Search
            # -----------------------------------------------------------------
            print("\n" + "=" * 60)
            print("TEST 3: search_knowledge_catalog() — Query Cases")
            print("=" * 60)

            test_queries = [
                "how to track visitor churn or immediate dropoff",  # Conceptually maps to bounce-rate
                "unique active people over past month",            # Conceptually maps to monthly-active-users
                "partitioned raw session logs and tables",         # Conceptually maps to ga-sessions-table-spec
            ]

            for query in test_queries:
                print(f"\n🔍 Query: '{query}'")
                res_search = await session.call_tool(
                    "search_knowledge_catalog",
                    arguments={"query": query}
                )
                print("Matches Found:")
                print(res_search.content[0].text)

            print("\n===================================================================")
            print(" 🎉 TESTING COMPLETE!")
            print("===================================================================")


if __name__ == "__main__":
    asyncio.run(run_mcp_tests())
